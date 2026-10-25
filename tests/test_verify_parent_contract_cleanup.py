"""Mocked ownership/cleanup controls; no Maven, signals, or listeners are executed."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SPEC = importlib.util.spec_from_file_location('parent_contract', Path(__file__).with_name('verify_parent_contract.py'))
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class Child:
    pid = 900001

    def __init__(self, complete=False, interrupt=False):
        self.returncode = 0 if complete else None
        self.interrupt = interrupt
        self.signals = []
        self.waits = []
        self.term_error = None
        self.wait_interrupt = False

    def poll(self):
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt('mock caller interruption')
        return self.returncode

    def send(self, pgid, sig):
        if pgid != self.pid:
            raise AssertionError('Only the Popen-created process group may be signaled')
        self.signals.append(sig)
        if sig == signal.SIGTERM and self.term_error:
            raise self.term_error

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if self.wait_interrupt:
            self.wait_interrupt = False
            raise KeyboardInterrupt('mock cleanup interruption')
        if self.signals and not (self.signals[-1] == signal.SIGTERM and self.term_error):
            self.returncode = -self.signals[-1]
            return self.returncode
        raise subprocess.TimeoutExpired('mock owned Maven', timeout)


class CleanupControls(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get('PARENT_CLEANUP_CONTROL_OUTPUT')
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.out = Path(tempfile.mkdtemp(prefix=self._testMethodName+'-', dir=parent))
        (self.out/'cache').mkdir()
        self.phase = self.out/'phase'
        self.phase.mkdir()

    def invoke(self, child, size_error=None, save_process_error=None, pgid=None, sid=None, result_error=None):
        real_save = runner.save
        size_calls = 0

        def measure(path):
            nonlocal size_calls
            size_calls += 1
            if size_error and size_calls == 1:
                raise size_error
            return 0

        def save(path, value):
            if save_process_error and path.name.endswith('-process.json'):
                raise save_process_error
            if result_error and path.name.endswith('-result.json'):
                raise result_error
            real_save(path, value)

        with patch.object(runner.subprocess, 'Popen', return_value=child) as popen, \
             patch.object(runner.os, 'killpg', side_effect=child.send) as kill, \
             patch.object(runner.os, 'getpgid', return_value=child.pid if pgid is None else pgid), \
             patch.object(runner.os, 'getsid', return_value=child.pid if sid is None else sid), \
             patch.object(runner, 'size', side_effect=measure), \
             patch.object(runner, 'save', side_effect=save), \
             patch.object(runner.time, 'sleep'), \
             patch.object(runner.time, 'monotonic', return_value=0):
            tools = {'maven':'mock-maven','javaHome':'mock-jdk'}
            result = runner.run(self.out, self.phase, 'mocked', ['-f', 'mock-pom.xml', 'org.codehaus.mojo:flatten-maven-plugin:1.7.0:flatten'], tools)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            return result, kill

    def assert_stopped_and_reaped(self, child):
        self.assertIsNotNone(child.returncode)
        self.assertTrue(child.waits)
        self.assertTrue(all(timeout is not None and timeout <= 5 for timeout in child.waits))

    def test_keyboard_interrupt_stops_and_reaps_owned_group(self):
        child = Child(interrupt=True)
        with self.assertRaisesRegex(KeyboardInterrupt, 'caller interruption'):
            self.invoke(child)
        self.assertIn(signal.SIGTERM, child.signals)
        self.assert_stopped_and_reaped(child)

    def test_monitor_size_exception_stops_and_reaps_owned_group(self):
        child = Child()
        failure = OSError('mock inaccessible size')
        with self.assertRaises(OSError) as caught:
            self.invoke(child, size_error=failure)
        self.assertIs(caught.exception, failure)
        self.assertIn(signal.SIGTERM, child.signals)
        self.assert_stopped_and_reaped(child)

    def test_process_metadata_save_exception_stops_and_reaps_owned_group(self):
        child = Child()
        failure = OSError('mock process evidence failure')
        with self.assertRaises(OSError) as caught:
            self.invoke(child, save_process_error=failure)
        self.assertIs(caught.exception, failure)
        self.assertIn(signal.SIGTERM, child.signals)
        self.assert_stopped_and_reaped(child)

    def test_term_failure_continues_to_verified_kill_and_reap(self):
        child = Child()
        child.term_error = OSError('mock TERM failure')
        with self.assertRaisesRegex(OSError, 'monitor failure'):
            self.invoke(child, size_error=OSError('mock monitor failure'))
        self.assertEqual(child.signals, [signal.SIGTERM, signal.SIGKILL])
        self.assert_stopped_and_reaped(child)

    def test_cleanup_wait_interruption_continues_to_verified_kill_and_reap(self):
        child = Child()
        child.wait_interrupt = True
        with self.assertRaisesRegex(OSError, 'monitor failure'):
            self.invoke(child, size_error=OSError('mock monitor failure'))
        self.assertEqual(child.signals, [signal.SIGTERM, signal.SIGKILL])
        self.assert_stopped_and_reaped(child)

    def test_completed_process_is_reaped_without_signals(self):
        child = Child(complete=True)
        result, kill = self.invoke(child)
        self.assertEqual(result[0], 0)
        kill.assert_not_called()
        self.assert_stopped_and_reaped(child)

    def test_unknown_group_is_not_signaled_but_bounded_reap_is_attempted(self):
        child = Child()
        with self.assertRaisesRegex(OSError, 'monitor failure'):
            self.invoke(child, size_error=OSError('mock monitor failure'), pgid=123456)
        self.assertEqual(child.signals, [])
        self.assertTrue(child.waits)
        self.assertTrue(all(timeout is not None and timeout <= 5 for timeout in child.waits))

    def test_unknown_session_is_not_signaled(self):
        child = Child()
        with self.assertRaisesRegex(OSError, 'monitor failure'):
            self.invoke(child, size_error=OSError('mock monitor failure'), sid=123456)
        self.assertEqual(child.signals, [])
        self.assertTrue(child.waits)

    def test_result_save_failure_does_not_bypass_reaping(self):
        child = Child(interrupt=True)
        with self.assertRaisesRegex(KeyboardInterrupt, 'caller interruption'):
            self.invoke(child, result_error=OSError('mock final evidence failure'))
        self.assertIn(signal.SIGTERM, child.signals)
        self.assert_stopped_and_reaped(child)


class ToolResolutionControls(unittest.TestCase):
    def setUp(self):
        parent = os.environ.get('PARENT_CLEANUP_CONTROL_OUTPUT')
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self.out = Path(tempfile.mkdtemp(prefix=self._testMethodName+'-', dir=parent))

    def fixture(self, platform='linux', maven_version='3.9.16', java_version='25.0.3'):
        maven = self.out/'maven'
        (maven/'bin').mkdir(parents=True)
        (maven/'lib').mkdir()
        (maven/'boot').mkdir()
        launcher = maven/'bin/mvn'
        launcher.write_text('#!/bin/sh\nexit 77\n')
        launcher.chmod(0o755)
        with zipfile.ZipFile(maven/'lib/maven-core-3.9.16.jar','w') as jar:
            jar.writestr('META-INF/maven/org.apache.maven/maven-core/pom.properties', 'version='+maven_version+'\n')
        (maven/'boot/plexus-classworlds.jar').write_bytes(b'mocked bootstrap input')
        java = self.out/'jdk'
        (java/'bin').mkdir(parents=True)
        for name in ['java','javac']:
            executable = java/'bin'/name
            executable.write_text('#!/bin/sh\nexit 77\n')
            executable.chmod(0o755)
        (java/'release').write_text('JAVA_VERSION="'+java_version+'"\n')
        (java/'lib/server').mkdir(parents=True)
        library = java/'lib/server'/('libjvm.dylib' if platform=='darwin' else 'libjvm.so')
        library.write_bytes(b'mocked JVM input')
        return launcher, java, library

    def test_explicit_macos_tools_pin_dylib_and_bootstrap_inputs(self):
        maven, java, library = self.fixture('darwin')
        tools = runner.resolve_tools(str(maven), java, {}, platform='darwin')
        self.assertEqual(tools['maven'], str(maven.resolve()))
        self.assertEqual(tools['javaHome'], str(java.resolve()))
        pins = runner.pin_tools(tools)
        self.assertIn(str(library.resolve()), pins)
        self.assertIn(str((maven.parent.parent/'boot/plexus-classworlds.jar').resolve()), pins)

    def test_explicit_linux_tools_pin_so_input(self):
        maven, java, library = self.fixture()
        tools = runner.resolve_tools(str(maven), java, {}, platform='linux')
        self.assertEqual(tools['jvmLibrary'], str(library.resolve()))
        self.assertIn(str(library.resolve()), runner.pin_tools(tools))

    def test_environment_path_and_java_home_are_supported(self):
        maven, java, _ = self.fixture()
        tools = runner.resolve_tools(environ={'PATH':str(maven.parent),'JAVA_HOME':str(java)}, platform='linux')
        self.assertEqual(tools['maven'], str(maven.resolve()))
        self.assertEqual(tools['javaHome'], str(java.resolve()))

    def test_explicit_arguments_override_invalid_environment_choices(self):
        maven, java, _ = self.fixture()
        tools = runner.resolve_tools(str(maven), java, {'MAVEN_EXECUTABLE':'missing','JAVA_HOME':'missing','PATH':''}, platform='linux')
        self.assertEqual(tools['maven'], str(maven.resolve()))

    def test_environment_maven_executable_overrides_path(self):
        maven, java, _ = self.fixture()
        tools = runner.resolve_tools(environ={'MAVEN_EXECUTABLE':str(maven),'JAVA_HOME':str(java),'PATH':''}, platform='linux')
        self.assertEqual(tools['maven'], str(maven.resolve()))

    def test_wrong_maven_metadata_version_is_rejected(self):
        maven, java, _ = self.fixture(maven_version='3.9.15')
        with self.assertRaisesRegex(ValueError, 'Maven 3.9.16'):
            runner.resolve_tools(str(maven), java, {}, platform='linux')

    def test_wrong_jdk_release_version_is_rejected(self):
        maven, java, _ = self.fixture(java_version='25.0.2')
        with self.assertRaisesRegex(ValueError, 'Java 25.0.3'):
            runner.resolve_tools(str(maven), java, {}, platform='linux')

    def test_missing_java_home_is_not_guessed(self):
        maven, _, _ = self.fixture()
        with self.assertRaisesRegex(ValueError, 'java-home|JAVA_HOME'):
            runner.resolve_tools(str(maven), environ={}, platform='linux')

    def test_unsupported_platform_fails_before_resolution(self):
        with self.assertRaisesRegex(ValueError, 'Unsupported platform'):
            runner.resolve_tools(environ={}, platform='win32')

    def test_missing_platform_jvm_library_is_rejected(self):
        maven, java, _ = self.fixture('darwin')
        with self.assertRaisesRegex(ValueError, 'libjvm.so'):
            runner.resolve_tools(str(maven), java, {}, platform='linux')

    def test_command_and_java_environment_use_resolved_inputs(self):
        maven, java, _ = self.fixture()
        tools = runner.resolve_tools(str(maven), java, {}, platform='linux')
        output = self.out/'evidence'
        args = ['-f','input.xml','-Prelease','org.codehaus.mojo:flatten-maven-plugin:1.7.0:flatten']
        self.assertEqual(runner.maven_command(output,args,tools), [str(maven.resolve()),'-B','-ntp','-s',str(output/'settings.xml'),'-gs',str(output/'settings.xml'),'-Dmaven.repo.local='+str(output/'cache'),*args])
        with patch.dict(runner.os.environ, {'MAVEN_ARGS':'unwanted','MAVEN_OPTS':'unwanted','JAVA_TOOL_OPTIONS':'unwanted','JDK_JAVA_OPTIONS':'unwanted'}, clear=True):
            env = runner.maven_environment(tools)
        self.assertEqual(env, {'JAVA_HOME':str(java.resolve()),'MAVEN_SKIP_RC':'true'})

    def test_cli_accepts_portable_tool_inputs(self):
        args = runner.parse_options(['--output',str(self.out/'evidence'),'--maven','mvn','--java-home',str(self.out/'jdk')])
        self.assertEqual(args.maven, 'mvn')
        self.assertEqual(args.java_home, self.out/'jdk')


if __name__ == '__main__':
    unittest.main()
