"""
Copyright 2015 BlazeMeter Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
import copy
import os
import time
from abc import abstractmethod

from urwid import Text, Pile

from bzt import TaurusConfigError, ToolError
from bzt.engine import FileLister, HavingInstallableTools, SelfDiagnosable
from bzt.modules import ReportableExecutor
from bzt.modules.console import WidgetProvider, PrioritizedWidget
from bzt.utils import get_files_recursive, get_full_path, RequiredTool, unzip, untar, shell_exec
from bzt.utils import is_windows, is_mac, platform_bitness, Environment

from bzt.commands import Commands
from bzt.resources.vnc_viewer.vncviewer import VncViewer
from multiprocessing import Process
import multiprocessing as mp
import requests
import shutil

try:
    mp.set_start_method('spawn', force=True)
except AttributeError:
    pass


class AbstractSeleniumExecutor(ReportableExecutor):
    @abstractmethod
    def get_virtual_display(self):
        """
        Return virtual display instance, if any.
        :return:
        """
        pass

    @abstractmethod
    def add_env(self, env):  # compatibility with taurus-server
        """
        Add environment variables into selenium process env
        :type env: dict[str,str]
        """
        pass


class ServiceAttached(object):
    service_attached = []

    remote = None

    @classmethod
    def get_attached(cls):
        return cls.service_attached

    @classmethod
    def add_attach(cls, attach_id):
        cls.service_attached.append(attach_id)

    @classmethod
    def detach(cls, attach_id):
        cls.service_attached.remove(attach_id)

    @classmethod
    def get_remote(cls, log):
        if cls.remote:
            return cls.remote
        else:
            cls.remote = Commands(log).remote
            return cls.remote


def run_vncviewer(host, port, password, conn_id):
    vnc_viewer = VncViewer(host, port, password, conn_id)
    return vnc_viewer


class SeleniumExecutor(AbstractSeleniumExecutor, WidgetProvider, FileLister, HavingInstallableTools, SelfDiagnosable):
    """
    Selenium executor
    :type runner: bzt.modules.SubprocessedExecutor
    """

    SUPPORTED_RUNNERS = ["nose", "junit", "testng", "rspec", "mocha", "nunit", "pytest", "wdio", "robot"]

    CHROMEDRIVER_DOWNLOAD_LINK = "https://chromedriver.storage.googleapis.com/{version}/chromedriver_{arch}.zip"
    CHROMEDRIVER_VERSION = "2.36"

    GECKODRIVER_DOWNLOAD_LINK = "https://github.com/mozilla/geckodriver/releases/download/v{version}/" \
                                "geckodriver-v{version}-{arch}.{ext}"
    GECKODRIVER_VERSION = "0.19.1"

    SELENIUM_TOOLS_DIR = get_full_path("~/.bzt/selenium-taurus/tools")

    def __init__(self):
        super(SeleniumExecutor, self).__init__()
        self.end_time = None
        self.runner = None
        self.script = None
        self.runner_working_dir = None
        self.register_reader = True
        self.webdrivers = []

        self.vnc_connections = []

    def add_env(self, env):  # compatibility with taurus-server
        self.env.set(env)

    def get_runner_working_dir(self):
        if self.runner_working_dir is None:
            self.runner_working_dir = self.engine.create_artifact("classes", "")
        return self.runner_working_dir

    def create_runner(self):

        runner_type = self.get_runner_type()
        self.runner = self.engine.instantiate_module(runner_type)
        self.runner.env = self.env

        # Propagate to Runner Remote capabilities if is used and generate Environment variables for external script
        service_remote = self.execution.get_noset("remote", self.settings.get_noset("remote", None))
        service_capabilities = self.execution.get_noset("capabilities", self.settings.get_noset("capabilities", []))
        use_service = self.execution.get_noset("service", self.settings.get_noset("service", None))

        service_video = self.execution.get_noset("service_video",
                                                 self.settings.get_noset("service_video", True))
        service_screenshot = self.execution.get_noset("service_screenshot",
                                                      self.settings.get_noset("service_screenshot", True))

        service_id = None
        service_vnc = None
        if use_service:
            service_info = ServiceAttached.get_remote(self.log).pull_service(use_service,
                                                                             ServiceAttached.get_attached(), cache=True)
            service_id = service_info["service_id"]
            if service_info["remote"]:
                ServiceAttached.add_attach(service_info["attach_id"])
                service_remote = service_info["remote"]
                service_capabilities = service_info["capabilities"]

            if service_info["vnc"] and self.settings.get_noset("service_vnc", True):
                service_vnc = service_info["vnc"]

        self.runner.parameters = self.parameters
        self.runner.provisioning = self.provisioning
        self.runner.execution = copy.deepcopy(self.execution)

        # Promote the resolution
        self.runner.execution["service_id"] = service_id
        self.runner.execution["remote"] = service_remote
        self.runner.execution["capabilities"] = service_capabilities
        self.runner.execution["vnc"] = service_vnc
        self.runner.execution["video"] = service_video
        self.runner.execution["screenshot"] = service_screenshot

        # TODO: For debug, remove
        self.log.info("Service:" + str(use_service))
        self.log.info("Remote:" + str(self.runner.execution["remote"]))
        self.log.info("Capabilities:" + str(len(self.runner.execution["capabilities"])))

        if self.env:
            if "remote" in self.runner.execution:
                self.add_env({"BZT_REMOTE": self.runner.execution["remote"]})
            if "capabilities" in self.runner.execution:
                for remote_cap in self.runner.execution["capabilities"]:
                    if "browser" in remote_cap:
                        self.add_env({"BZT_REMOTE_BROWSER": remote_cap["browser"]})

        self.runner.execution['files'] = self.execution.get('files', [])
        self.runner.execution['executor'] = runner_type
        self.runner.register_reader = self.register_reader

        if runner_type == "nose":
            self.runner.execution["test-mode"] = "selenium"

    def get_virtual_display(self):
        pass  # for compatibility with taurus server

    def _get_chromedriver_link(self):
        settings = self.settings.get('chromedriver')
        link = settings.get('download-link', SeleniumExecutor.CHROMEDRIVER_DOWNLOAD_LINK)
        version = settings.get('version', SeleniumExecutor.CHROMEDRIVER_VERSION)
        if is_windows():
            arch = 'win32'  # no 64-bit windows builds, :(
        elif is_mac():
            arch = 'mac64'
        else:
            arch = 'linux32' if platform_bitness() == 32 else 'linux64'
        return link.format(version=version, arch=arch)

    def _get_chromedriver_path(self):
        base_dir = get_full_path(SeleniumExecutor.SELENIUM_TOOLS_DIR)
        settings = self.settings.get('chromedriver')
        version = settings.get('version', SeleniumExecutor.CHROMEDRIVER_VERSION)
        filename = 'chromedriver.exe' if is_windows() else 'chromedriver'
        return os.path.join(base_dir, 'chromedriver', version, filename)

    def _get_geckodriver_link(self):
        settings = self.settings.get('geckodriver')
        link = settings.get('download-link', SeleniumExecutor.GECKODRIVER_DOWNLOAD_LINK)
        version = settings.get('version', SeleniumExecutor.GECKODRIVER_VERSION)
        if is_windows():
            arch = 'win64'  # no 32-bit windows builds, :(
            ext = 'zip'
        elif is_mac():
            arch = 'macos'
            ext = 'tar.gz'
        else:
            arch = 'linux32' if platform_bitness() == 32 else 'linux64'
            ext = 'tar.gz'
        return link.format(version=version, arch=arch, ext=ext)

    def _get_geckodriver_path(self):
        base_dir = get_full_path(SeleniumExecutor.SELENIUM_TOOLS_DIR)
        settings = self.settings.get('geckodriver')
        version = settings.get('version', SeleniumExecutor.GECKODRIVER_VERSION)
        filename = 'geckodriver.exe' if is_windows() else 'geckodriver'
        return os.path.join(base_dir, 'geckodriver', version, filename)

    def install_required_tools(self):
        chromedriver_path = self._get_chromedriver_path()
        chromedriver_link = self._get_chromedriver_link()
        geckodriver_path = self._get_geckodriver_path()
        geckodriver_link = self._get_geckodriver_link()

        self.webdrivers = [ChromeDriver(chromedriver_path, self.log, chromedriver_link),
                           GeckoDriver(geckodriver_path, self.log, geckodriver_link)]

        for tool in self.webdrivers:
            if not tool.check_if_installed():
                self.log.info("Installing %s...", tool.tool_name)
                tool.install()

    def prepare(self):
        if self.env is None:
            self.env = Environment(self.log, self.engine.env.get())  # for backward compatibility with taurus-server

        self.install_required_tools()
        for driver in self.webdrivers:
            self.env.add_path({"PATH": driver.get_driver_dir()})

        if self.get_load().concurrency and self.get_load().concurrency > 1:
            msg = 'Selenium supports concurrency in cloud provisioning mode only\n'
            msg += 'For details look at http://gettaurus.org/docs/Cloud.md'
            self.log.warning(msg)

        self.create_runner()
        self.runner.prepare()
        self.script = self.runner.script

    def get_runner_type(self):
        if "runner" in self.execution:
            runner = self.execution["runner"]
            if runner not in SeleniumExecutor.SUPPORTED_RUNNERS:
                msg = "Runner '%s' is not supported. Supported runners: %s"
                raise TaurusConfigError(msg % (runner, SeleniumExecutor.SUPPORTED_RUNNERS))
            self.log.debug("Using script type: %s", runner)
            return runner

        script_name = self.get_script_path()
        if script_name:
            return self.detect_script_type(script_name)
        else:
            if "requests" in self.get_scenario():
                return "nose"
            else:
                raise TaurusConfigError("You must specify either script or list of requests to run Selenium")

    def resource_files(self):
        self.create_runner()
        return self.runner.resource_files()

    def detect_script_type(self, script_name):
        if not os.path.exists(script_name):
            raise TaurusConfigError("Script '%s' doesn't exist" % script_name)

        file_types = set()

        # gather file extensions and choose script_type according to priority
        if os.path.isfile(script_name):  # regular file received
            file_types.add(os.path.splitext(script_name)[1].lower())
        else:  # dir received: check contained files
            for file_name in get_files_recursive(script_name):
                file_types.add(os.path.splitext(file_name)[1].lower())

        if '.java' in file_types or '.jar' in file_types:
            # todo: next detection logic is duplicated in TestNGTester - can we avoid it?
            script_dir = get_full_path(self.get_script_path(), step_up=1)
            if os.path.exists(os.path.join(script_dir, 'testng.xml')) or self.execution.get('testng-xml', None):
                script_type = 'testng'
            else:
                script_type = 'junit'
        elif '.py' in file_types:
            script_type = 'nose'
        elif '.rb' in file_types:
            script_type = 'rspec'
        elif '.js' in file_types:
            script_type = 'mocha'
        elif '.dll' in file_types or '.exe' in file_types:
            script_type = 'nunit'
        else:
            if os.path.isfile(script_name):
                message = "Unsupported script type: %r" % script_name
            else:
                message = "Directory %r doesn't contain supported scripts" % script_name
            raise TaurusConfigError(message)

        self.log.debug("Detected script type: %s", script_type)

        return script_type

    def startup(self):
        """
        Start runner
        :return:
        """
        self.start_time = time.time()

        if self.runner.execution["vnc"]:
            vnc_host = self.runner.execution["vnc"].split(":")[0]
            vnc_port = int(self.runner.execution["vnc"].split(":")[1])
            vnc_pass = "secret"

            if is_mac():
                cmdline = ["open", "vnc://user:%s@%s:%d" % (vnc_pass, vnc_host, vnc_port)]
                vnc_proc = shell_exec(cmdline)
            else:
                vnc_proc = Process(target=run_vncviewer, args=(vnc_host, vnc_port, vnc_pass,
                                                               self.runner.execution["service_id"],))
                vnc_proc.daemon = True
                vnc_proc.start()

            self.vnc_connections.append(vnc_proc)

        if self.runner.execution["remote"]:
            service_host = self.runner.execution["remote"].split(":")[1]
            service_url = self.runner.execution["remote"].split(":")[0] + ':' + service_host + \
                          ':5555/extra/bzt_servlet?command=startTest'

            video = self.runner.execution.get_noset("video", self.runner.settings.get_noset("video", False))
            screenshot = self.runner.execution.get_noset("screenshot", self.runner.settings.get_noset("screenshot", False))

            first_connetion_timeout = 3.05  # Slightly larger than 3, default TCP packet retransmission window.
            first_reponse_timeout = 6
            try:
                response = requests.post(service_url,
                                         json={"enableVideo": video, "enableScreenshot": screenshot},
                                         timeout=(first_connetion_timeout, first_reponse_timeout))
                if response.status_code == 200:
                    self.log.info("Service StartTest")
            except requests.exceptions.RequestException as e:
                self.log.info("Service without StartTest")

        self.runner.startup()

    def check(self):
        """
        check if test completed
        :return:
        """
        if self.widget:
            self.widget.update()

        return self.runner.check()

    def report_test_duration(self):
        if self.start_time:
            self.end_time = time.time()
            self.log.debug("Selenium tests ran for %s seconds", self.end_time - self.start_time)

    def service_sync_artifacts(self):
        self.log.info("Service:" + str(self.runner.execution["service_id"]))
        self.log.info("Remote:" + str(self.runner.execution["remote"]))
        self.log.info("Capabilities:" + str(len(self.runner.execution["capabilities"])))

        first_connetion_timeout = 3.05  # Slightly larger than 3, default TCP packet retransmission window.
        first_reponse_timeout = 6
        if self.runner.execution["remote"]:
            service_host = self.runner.execution["remote"].split(":")[1]
            service_url = self.runner.execution["remote"].split(":")[0] + \
                          ':' + service_host + \
                          ':5555/extra/bzt_servlet?command=endTest'

            try:
                response = requests.post(service_url,
                                         json={},
                                         timeout=(first_connetion_timeout, first_reponse_timeout))

                if response.status_code == 200:
                    self.log.info("Service EndTest")
                    service_url = self.runner.execution["remote"].split(":")[
                                      0] + ":" + service_host + ":5555/extra/bzt_servlet"
                    request = requests.get(service_url, stream=True,
                                           timeout=(first_connetion_timeout, first_reponse_timeout))
                    self.log.info("Script:" + self.script)
                    base_path_script = '.'.join(self.script.split('.')[:-1])
                    execution_artifacts_file = base_path_script + ".zip"
                    with open(execution_artifacts_file, 'wb') as f:
                        shutil.copyfileobj(request.raw, f)
            except requests.exceptions.RequestException as e:
                self.log.info("Service without endTest")

    def shutdown(self):
        """
        shutdown test_runner
        :return:
        """
        self.runner.shutdown()
        self.report_test_duration()

    def post_process(self):
        self.runner.post_process()
        self.service_sync_artifacts()
        if os.path.exists("geckodriver.log"):
            self.engine.existing_artifact("geckodriver.log", True)

    def has_results(self):
        return self.runner.has_results()

    def get_widget(self):
        if not self.widget:
            self.widget = SeleniumWidget(self.script, self.runner.stdout_file)
        return self.widget

    def get_error_diagnostics(self):
        diagnostics = []
        if self.runner:
            diagnostics.extend(self.runner.get_error_diagnostics())
        gecko_logs = ["geckodriver.log", os.path.join(self.engine.artifacts_dir, "geckodriver.log")]
        for possible_log in gecko_logs:
            if os.path.exists(possible_log):
                with open(possible_log) as fds:
                    diagnostics.append("Geckodriver log:\n" + fds.read())
        return diagnostics


class SeleniumWidget(Pile, PrioritizedWidget):
    def __init__(self, script, runner_output):
        widgets = []
        self.script_name = Text("Selenium: %s" % os.path.basename(script))
        self.summary_stats = Text("Delayed...")
        self.runner_output = runner_output
        widgets.append(self.script_name)
        widgets.append(self.summary_stats)
        super(SeleniumWidget, self).__init__(widgets)
        PrioritizedWidget.__init__(self, priority=10)

    def update(self):
        reader_summary = ''
        if self.runner_output is not None and os.path.exists(self.runner_output):
            with open(self.runner_output, "rt") as fds:
                lines = fds.readlines()
                if lines:
                    line = lines[-1]
                    if not line.endswith("\n") and len(lines) > 1:
                        line = lines[-2]
                    if line and "," in line:
                        reader_summary = line.split(",")[-1]

        if reader_summary:
            self.summary_stats.set_text(reader_summary)
        else:
            self.summary_stats.set_text('In progress...')

        self._invalidate()


class ChromeDriver(RequiredTool):
    def __init__(self, tool_path, parent_logger, download_link):
        super(ChromeDriver, self).__init__("ChromeDriver", tool_path, download_link)
        self.log = parent_logger.getChild(self.__class__.__name__)

    def check_if_installed(self):
        return os.path.exists(self.tool_path)

    def get_driver_dir(self):
        return get_full_path(self.tool_path, step_up=1)

    def install(self):
        dest = self.get_driver_dir()
        if not os.path.exists(dest):
            os.makedirs(dest)

        self.log.info("Will install %s into %s", self.tool_name, dest)
        dist = self._download(use_link=True)
        try:
            self.log.info("Unzipping %s to %s", dist, dest)
            unzip(dist, dest)
        finally:
            os.remove(dist)

        if not is_windows():
            os.chmod(self.tool_path, 0o755)

        if not self.check_if_installed():
            raise ToolError("Unable to find %s after installation!" % self.tool_name)


class GeckoDriver(RequiredTool):
    def __init__(self, tool_path, parent_logger, download_link):
        super(GeckoDriver, self).__init__("GeckoDriver", tool_path, download_link)
        self.log = parent_logger.getChild(self.__class__.__name__)

    def check_if_installed(self):
        return os.path.exists(self.tool_path)

    def get_driver_dir(self):
        return get_full_path(self.tool_path, step_up=1)

    def install(self):
        dest = self.get_driver_dir()
        if not os.path.exists(dest):
            os.makedirs(dest)

        self.log.info("Will install %s into %s", self.tool_name, dest)
        dist = self._download(use_link=True)
        try:
            if self.download_link.endswith('.zip'):
                self.log.info("Unzipping %s to %s", dist, dest)
                unzip(dist, dest)
            else:
                self.log.info("Untaring %s to %s", dist, dest)
                untar(dist, dest)
        finally:
            os.remove(dist)

        if not is_windows():
            os.chmod(self.tool_path, 0o755)

        if not self.check_if_installed():
            raise ToolError("Unable to find %s after installation!" % self.tool_name)

        # TODO: check for compatible browser versions?
