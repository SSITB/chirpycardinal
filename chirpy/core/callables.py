import os
import json
import time
import logging
import sys
import traceback
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path

import requests

from chirpy.core import flags
from typing import Callable, Any, Optional, List, Dict, Type, Set
from datetime import datetime
from dataclasses import dataclass

from chirpy.core.state_manager import StateManager
from chirpy.core.util import run_module

logger = logging.getLogger('chirpylogger')
CHIRPY_HOME = os.environ.get('CHIRPY_HOME', Path(__file__).parent.parent.parent)


def get_url(name):
    url = os.environ.get(f'{name}_URL', None)
    logger.debug(f"For callable: {name} got remote url: {url}")
    return url

class RemoteCallableError(Exception):
    def __init__(self, message: str):
        logger.error(message)

class NamedCallable:
    # Should have a class variable for name
    name = None
    pass

class RemoteCallable(NamedCallable):
    def __init__(self,  url: str, timeout: float = flags.inf_timeout):
        self.url = url
        self.timeout = timeout if flags.use_timeouts else flags.inf_timeout

    def default_fn(self, input_data):
        return None

    def __call__(self, input_data):
        """
        Query the url using on the serialized arguments.
        Returns the output in json dict format, or default_fn if there is an error.
        the default_fn is expected to NOT throw an error. If it does, another error is logged
        and None is returned
        """
        start = datetime.now()
        data = json.dumps(input_data)
        logger.info(f"RemoteCallable {self.name} is sending data {data} with timeout = {self.timeout} seconds to url {self.url}")
        try:
            try:
                response = requests.post(self.url,
                                         data=data,
                                         headers={'content-type': 'application/json'},
                                         timeout=self.timeout)
                end = datetime.now()
                logger.info("RemoteCallable {} got result={}, latency: {}ms. Now will convert response to json.".format(self.name, response, (end - start).total_seconds() * 1000))

                # If the response has an error code, raise the readable error
                if not response.ok:
                    response.raise_for_status()

                # Otherwise get the json representation of the contents
                response = response.json()
                logger.info("RemoteCallable {} finished. result: {}, latency: {}ms".format(self.name, response, (end - start).total_seconds() * 1000))
                if isinstance(response, dict) and 'error' in response and response['error']:
                    raise RemoteCallableError("RemoteCallable {} returned a result with error=True: {}".format(self.name, response))
                else:
                    return response

            # See here for types of errors: https://requests.readthedocs.io/en/master/api/#exceptions
            except RemoteCallableError:
                return self.default_fn(input_data)
            except requests.exceptions.Timeout as e:
                logger.warning(f'RemoteCallable timed out when running {self.name} with timeout = {self.timeout} '
                             f'seconds \n and data={data}')  # don't include stack trace for timeouts
                return self.default_fn(input_data)
            except requests.exceptions.HTTPError as e:
                logger.error(f'RemoteCallable returned a HTTPError when running {self.name}: {e}')
                return self.default_fn(input_data)

            except Exception as e:
                logger.error(f'RemoteCallable encountered an error when running {self.name} with timeout ='
                             f' {self.timeout} seconds and data={data}', exc_info=True)
                return self.default_fn(input_data)

        except:
            logger.error("RemoteCallable default_fn threw an error", exc_info=True)
            return None

class Annotator(RemoteCallable):
    def __init__(self, state_manager: StateManager, timeout: float, url: str = None, input_annotations :List[str] = []):
        if url is None:
            url = get_url(self.name)
        super().__init__(url = url, timeout=timeout)
        self.state_manager = state_manager
        self.input_annotations = input_annotations

    def save_to_state(self, value):
        setattr(self.state_manager.current_state, self.name, value)

    def remote_call(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)

    def default_fn(self, input_data):
        return self.get_default_response()

    def execute(self, input_data):
        raise NotImplementedError

    def get_default_response(self, input_data):
        raise NotImplementedError


class ResponseGenerator(NamedCallable):
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager

def run_multithreaded(module_instance: List[NamedCallable],
                      function_name:str,
                      timeout: Optional[float]=None,
                      args_list: Optional[List[List]]=None,
                      kwargs_list: Optional[List[Dict]]=None):
        # Can't use a context manager (with .. as ..) because
        # it will ensure termination of running threads even when they
        # exceed desired timeout
        max_workers = len(module_instance)
        if max_workers == 0:
            return {}
        logger.debug(f'Initializing ThreadPoolExecutor with max_workers={max_workers}')
        executor = ThreadPoolExecutor(max_workers=max_workers)
        result = {}
        if args_list is None:
            args_list = [[] for _ in module_instance]
        if kwargs_list is None:
            kwargs_list = [{} for _ in module_instance]

        future_to_module_name = {executor.submit(run_module, module, function_name, args, kwargs): module.name
                                 for (module, args, kwargs) in zip(module_instance, args_list, kwargs_list)}

        try:
            for future in futures.as_completed(future_to_module_name, timeout=timeout):
                module_name = future_to_module_name[future]
                try:
                    future_result = future.result()
                    result[module_name] = future_result
                except requests.exceptions.Timeout:
                    logger.warning(f"Timed out when running module {module_name} with function "
                                   f"{function_name}. So {module_name} will be missing from state.")
                except Exception:
                    try:
                        exception_type, exception_value, tb = sys.exc_info()
                        localized_stacktrace = traceback.extract_tb(tb)[-1]
                        filename, line_number, function_name, text = localized_stacktrace
                        logger.exception(f"Encountered {exception_value.__repr__()} within `{function_name}` at {filename}:{line_number} in `{text}` when running function `{function_name}` of module `{module_name}`")
                    except Exception:
                        logger.exception(f"Encountered error when running function `{function_name}` of module `{module_name}`")
        except TimeoutError as e:
            for future in future_to_module_name:
                module_name = future_to_module_name[future]
                if future.running():
                    logger.error(
                        f"Timed out when running function {function_name} of module {module_name} with timeout = {timeout} seconds")
                future.cancel()

        return result

def run_multithreaded_DAG(module_instances: List[Annotator],
                      function_name:str,
                      timeout: Optional[float]=None,
                      args_list: Optional[List[List]]=None,
                      kwargs_list: Optional[List[Dict]]=None):
    max_workers = len(module_instances)
    if max_workers == 0:
        return {}
    logger.debug(f'Initializing ThreadPoolExecutor with max_workers={max_workers}')
    executor = ThreadPoolExecutor(max_workers=max_workers)
    result = {}
    args_list = args_list or [[] for _ in module_instances]
    kwargs_list = kwargs_list or [{} for _ in module_instances]

    # List of modules names which have some response (includes default responses)
    succeeded_modules = set()

    # Set of module names which have completely failed
    failed_modules = set()

    # Dictionary from module instances to argument list
    module_2_args = {module_instance: (args_list, kwargs_list) for module_instance, args_list, kwargs_list in
                     zip(module_instances, args_list, kwargs_list)}

    # Initialize list of unexecuted module instances with all the modules
    unexecuted_modules = list(module_2_args.keys())

    # Dictionary from future to module name, initialized to be empty
    future_to_module = {}
    begin_time = time.perf_counter_ns()
    while unexecuted_modules or future_to_module:
        # Get modules that can be executed
        executable_modules, unexecuted_modules, failed_modules = \
            get_ready_callables(succeeded_modules, failed_modules, unexecuted_modules)

        # Schedule executable modules to run
        future_to_module.update({executor.submit(run_module, module, function_name,
                                                 module_2_args[module][0],
                                                 module_2_args[module][1]): module
                                 for module in executable_modules})

        time_elapsed = ((time.perf_counter_ns() - begin_time) / 1000000000)
        next_timeout = timeout and timeout - time_elapsed

        # If there is no time remaining, get default response for all remaining modules and break out of the loop
        if next_timeout <= 0:
            logger.error(f"NLP pipeline hit overall timeout in {time_elapsed} "
                         f"seconds. ")

            for module in unexecuted_modules + list(future_to_module.values()):
                module_name = module.name
                try:
                    default_response = module.get_default_response()
                    result[module_name] = default_response
                    # Add the result to state_manager so that it can be used by subsequent annotators
                    module.save_to_state(default_response)
                    logger.primary_info(f"Using default response for {module_name}: {result[module_name]}")
                    succeeded_modules.add(module_name)
                except:
                    logger.error(f"ServiceModule encountered an error when running {module_name}'s "
                                 f"get_default_response function", exc_info=True)
                    failed_modules.add(module_name)

            # Cancel futures in case they happen to have not been scheduled
            for future in future_to_module:
                future.cancel()

            break

        # Wait till the first future is complete, it'll come as done. If timeout is hit, done is empty
        done, not_done = futures.wait(future_to_module, timeout=next_timeout, return_when=futures.FIRST_COMPLETED)

        for future in done:
            # Get the module name and remove it from the list of futures we will wait on in the future
            module = future_to_module.pop(future)
            module_name = module.name

            try:
                future_result = future.result()
                result[module_name] = future_result
                # Add the result to state_manager so that it can be used by subsequent annotators
                module.save_to_state(future_result)
                logger.info(f"Succesfully executed {module_name}")
                succeeded_modules.add(module_name)
            except Exception:
                logger.warning(f"Failed to execute {module_name}", exc_info=True)
                failed_modules.add(module_name)

        # set of succeeded and failed modules have been updated.
        # Repeat the loop and re-evaulate which un-executed modules can be scheduled next
    logger.primary_info("CallModules summary: \n" +
                        (f"MODULES WITH SOME RESPONSE: {', '.join(succeeded_modules)}\n"
                         if succeeded_modules else '') +
                        (f"FAILED MODULES: {', '.join(failed_modules)}"
                         if failed_modules else ''))

    return result


def get_ready_callables(succeeded_modules: Set[str], failed_modules: Set[str], unexecuted_modules: List[Annotator]):
    """ Get unexecuted modules which can be executed, based on whether their requirements are satisfied.
        If their requirements have failed, then add them to failed modules as well.

    Args:
        succeeded_modules (Set[str]): Set of modules names which have successfully completed
        failed_modules (Set[str]): Set of module names which have failed (errored out or timed out)
        unexecuted_modules (List[Module]: List of modules yet to be executed

    Returns:
        executable_modules (List[Module]): Modules whose requirements are met and are ready to be executed
        unexecutable_modules (List[Module]): Modules whose requirements are unmet but might be met in the future
        failed_modules (Set[string]): Module names for modules which have failed by themselves
                                        or because their requirements have failed

    """
    executable_modules = []
    unexecutable_modules = []
    for module in unexecuted_modules:
        requirements = module.input_annotations
        if len(set(requirements) - succeeded_modules) == 0:
            executable_modules.append(module)
            logger.info(f"Ready to execute {module.name} as its module requirements = {requirements} are satisfied")
        elif len(set(requirements) - (succeeded_modules | failed_modules)) == 0:
            failed_modules.add(module.name)
            logger.info(f"Failed to execute {module.name} as its module requirements "
                        f"{failed_modules & set(requirements)} also failed to execute")
        else:
            unexecutable_modules.append(module)
    return executable_modules, unexecutable_modules, failed_modules


class ResponseGenerators:
    def __init__(self,  state_manager: StateManager, rg_classes: List[Type[ResponseGenerator]]):
        self.name_to_class = {rg_class.name: rg_class for rg_class in rg_classes}
        self.state_manager = state_manager

    def run_multithreaded(self, rg_names: List[str],
                          function_name:str,
                          timeout: Optional[float]=None,
                          args_list: Optional[List[List]]=None,
                          kwargs_list: Optional[List[Dict]]=None):
        assert set(rg_names).issubset(set(self.name_to_class)), f"{set(rg_names) - set(self.name_to_class)} not found in ResponseGenerators"
        rg_objs = [self.name_to_class[rg_name](self.state_manager) for rg_name in rg_names]
        return run_multithreaded(rg_objs, function_name, timeout, args_list, kwargs_list)






class AnnotationDAG:
    def __init__(self, state_manager: StateManager, annotators: List[Annotator], timeout: float):
        self.name_2_annotators = {a.name: a for a in annotators}

        for annotator in annotators:
            dependencies = annotator.input_annotations
            unmet_dependencies = set(dependencies) - set(self.name_2_annotators)
            if len(unmet_dependencies) > 0:
                raise Exception(f"Input annotators ({unmet_dependencies}) for annotator {annotator.name} do not exist in "
                                f"Annotators")
        self.annotators = annotators
        self.state_manager = state_manager
        self.timeout = timeout

    def run_multithreaded_DAG(self):
        return run_multithreaded_DAG(self.annotators, 'execute', self.timeout)

        # In the future, if required, also check for cyclic dependencies here




