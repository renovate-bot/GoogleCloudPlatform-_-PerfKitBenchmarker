# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Set of utility functions for working with virtual machines."""


import contextlib
import enum
import logging
import os
import platform
import posixpath
import random
import re
import string
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, Tuple

from absl import flags
import jinja2
from perfkitbenchmarker import data
from perfkitbenchmarker import errors
from perfkitbenchmarker import temp_dir

FLAGS = flags.FLAGS
# Using logger rather than logging.info to avoid stack_level problems.
logger = logging.getLogger()

PRIVATE_KEYFILE = 'perfkitbenchmarker_keyfile'
PUBLIC_KEYFILE = 'perfkitbenchmarker_keyfile.pub'

# The temporary directory on VMs. We cannot reuse GetTempDir()
# because run_uri will not be available at time of module load and we need
# to use this directory as a base for other module level constants.
VM_TMP_DIR = '/tmp/pkb'

# Default timeout for issuing a command.
DEFAULT_TIMEOUT = 300

# Defaults for retrying commands.
POLL_INTERVAL = 30
TIMEOUT = 1200
FUZZ = 0.5
MAX_RETRIES = -1

WINDOWS = 'nt'
DARWIN = 'Darwin'
PASSWORD_LENGTH = 15

OUTPUT_STDOUT = 0
OUTPUT_STDERR = 1
OUTPUT_EXIT_CODE = 2

flags.DEFINE_integer(
    'default_timeout',
    TIMEOUT,
    'The default timeout for retryable commands in seconds.',
)
flags.DEFINE_integer(
    'burn_cpu_seconds',
    0,
    'Amount of time in seconds to burn cpu on vm before starting benchmark',
)
flags.DEFINE_integer(
    'burn_cpu_threads',
    1,
    'Number of threads to use to burn cpu before starting benchmark.',
)
flags.DEFINE_integer(
    'background_cpu_threads',
    None,
    'Number of threads of background vm_util.cpu usage while '
    'running a benchmark',
)
flags.DEFINE_integer(
    'background_network_mbits_per_sec',
    None,
    'Number of megabits per second of background '
    'network traffic to generate during the run phase '
    'of the benchmark',
)
flags.DEFINE_boolean(
    'ssh_reuse_connections',
    True,
    'Whether to reuse SSH connections rather than '
    'reestablishing a connection for each remote command.',
)
# We set this to the short value of 5 seconds so that the cluster boot benchmark
# can measure a fast connection when bringing up a VM. This avoids retries that
# may not be as quick as every 5 seconds when specifying a larger value.
flags.DEFINE_integer(
    'ssh_connect_timeout', 5, 'timeout for SSH connection.', lower_bound=0
)
flags.DEFINE_string(
    'ssh_control_path',
    None,
    'Overrides the default ControlPath setting for ssh '
    'connections if --ssh_reuse_connections is set. This can '
    'be helpful on systems whose default temporary directory '
    'path is too long (sockets have a max path length) or a '
    "version of ssh that doesn't support the %h token. See "
    'ssh documentation on the ControlPath setting for more '
    'detailed information.',
)
flags.DEFINE_string(
    'ssh_control_persist',
    '30m',
    'Setting applied to ssh connections if '
    '--ssh_reuse_connections is set. Sets how long the '
    'connections persist before they are removed. '
    'See ssh documentation about the ControlPersist setting '
    'for more detailed information.',
)
flags.DEFINE_integer(
    'ssh_server_alive_interval',
    30,
    'Value for ssh -o ServerAliveInterval. Use with '
    '--ssh_server_alive_count_max to configure how long to '
    'wait for unresponsive servers.',
)
flags.DEFINE_integer(
    'ssh_server_alive_count_max',
    10,
    'Value for ssh -o ServerAliveCountMax. Use with '
    '--ssh_server_alive_interval to configure how long to '
    'wait for unresponsive servers.',
)
_SSH_PUBLIC_KEY = flags.DEFINE_string(
    'ssh_public_key',
    None,
    'File path to the SSH public key. If None, use the newly generated one.',
)
_SSH_PRIVATE_KEY = flags.DEFINE_string(
    'ssh_private_key',
    None,
    'File path to the SSH private key. If None, use the newly generated one.',
)


class RetryError(Exception):
  """Base class for retry errors."""


class TimeoutExceededRetryError(RetryError):
  """Exception that is raised when a retryable function times out."""


class RetriesExceededRetryError(RetryError):
  """Exception that is raised when a retryable function hits its retry limit."""


class ImageNotFoundError(Exception):
  """Exception that is raised when an image is not found."""


class IpAddressSubset:
  """Enum of options for --ip_addresses."""

  REACHABLE = 'REACHABLE'
  BOTH = 'BOTH'
  INTERNAL = 'INTERNAL'
  EXTERNAL = 'EXTERNAL'

  ALL = (REACHABLE, BOTH, INTERNAL, EXTERNAL)


@enum.unique
class VmCommandLogMode(enum.Enum):
  """The log mode for vm_util.IssueCommand function."""

  ALWAYS_LOG = 'always_log'
  LOG_ON_ERROR = 'log_on_error'


_VM_COMMAND_LOG_MODE = flags.DEFINE_enum_class(
    'vm_command_log_mode',
    VmCommandLogMode.ALWAYS_LOG,
    VmCommandLogMode,
    (
        'Controls the logging behavior of vm_util.IssueCommand, and'
        ' specifically its full log statement including output & error message.'
    ),
)


flags.DEFINE_enum(
    'ip_addresses',
    IpAddressSubset.INTERNAL,
    IpAddressSubset.ALL,
    'For networking tests: use both internal and external '
    'IP addresses (BOTH), internal and external only if '
    'the receiving VM is reachable by internal IP (REACHABLE), '
    'external IP only (EXTERNAL) or internal IP only (INTERNAL). The default '
    'is set to INTERNAL.',
)

flags.DEFINE_enum(
    'background_network_ip_type',
    IpAddressSubset.EXTERNAL,
    (IpAddressSubset.INTERNAL, IpAddressSubset.EXTERNAL),
    'IP address type to use when generating background network traffic',
)


class IpAddressMetadata:
  INTERNAL = 'internal'
  EXTERNAL = 'external'


def UseProvidedSSHKeys():
  if (
      _SSH_PUBLIC_KEY.value
      and _SSH_PRIVATE_KEY.value
      and os.path.isfile(_SSH_PUBLIC_KEY.value)
      and os.path.isfile(_SSH_PRIVATE_KEY.value)
  ):
    return True
  return False


def GetTempDir():
  """Returns the tmp dir of the current run."""
  return temp_dir.GetRunDirPath()


def PrependTempDir(file_name):
  """Returns the file name prepended with the tmp dir of the current run."""
  return os.path.join(GetTempDir(), file_name)


def GenTempDir():
  """Creates the tmp dir for the current run if it does not already exist."""
  temp_dir.CreateTemporaryDirectories()


def SSHKeyGen():
  """Use provided SSH keys or create PerfKitBenchmarker SSH keys in the tmp dir of the current run."""
  if UseProvidedSSHKeys():
    return

  if not os.path.isdir(GetTempDir()):
    GenTempDir()

  if not os.path.isfile(GetPrivateKeyPath()):
    create_cmd = [
        'ssh-keygen',
        '-t',
        'rsa',
        '-N',
        '',
        '-m',
        'PEM',
        '-q',
        '-f',
        PrependTempDir(PRIVATE_KEYFILE),
    ]
    IssueCommand(create_cmd)


def GetPrivateKeyPath():
  if UseProvidedSSHKeys():
    return _SSH_PRIVATE_KEY.value
  return PrependTempDir(PRIVATE_KEYFILE)


def GetPublicKeyPath():
  if UseProvidedSSHKeys():
    return _SSH_PUBLIC_KEY.value
  return PrependTempDir(PUBLIC_KEYFILE)


def IncrementStackLevel(**kwargs: Any) -> Any:
  """Increments the stack_level variable stored in kwargs.

  This method should be called from "helper" functions whose usage is not
  particularly interesting, but whose callers are.
  A default of 1 (before being incremented) represents the caller of the
  "helper" function.
  Args:
    **kwargs: The dictionary of arguments to modify, which may or may not
      contain stack_level.

  Returns:
    The modified dictionary of arguments.
  """
  if 'stack_level' not in kwargs:
    kwargs['stack_level'] = 1
  kwargs['stack_level'] += 1
  return kwargs


def GetSshOptions(ssh_key_filename, connect_timeout=None):
  """Return common set of SSH and SCP options."""
  # pyformat: disable
  options = [
      '-2',
      '-o', 'UserKnownHostsFile=/dev/null',
      '-o', 'StrictHostKeyChecking=no',
      '-o', 'IdentitiesOnly=yes',
      '-o', 'PreferredAuthentications=publickey',
      '-o', 'PasswordAuthentication=no',
      '-o', f'ConnectTimeout={connect_timeout or FLAGS.ssh_connect_timeout}',
      '-o', 'GSSAPIAuthentication=no',
      '-o', f'ServerAliveInterval={FLAGS.ssh_server_alive_interval}',
      '-o', f'ServerAliveCountMax={FLAGS.ssh_server_alive_count_max}',
      '-i', ssh_key_filename,
  ]
  # pyformat: enable
  if FLAGS.use_ipv6:
    options.append('-6')
  if FLAGS.ssh_reuse_connections:
    control_path = FLAGS.ssh_control_path or os.path.join(
        temp_dir.GetSshConnectionsDir(), '%h'
    )
    options.extend([
        '-o',
        'ControlPath="%s"' % control_path,
        '-o',
        'ControlMaster=auto',
        '-o',
        'ControlPersist=%s' % FLAGS.ssh_control_persist,
    ])
  options.extend(FLAGS.ssh_options)

  return options


def Retry(
    poll_interval=POLL_INTERVAL,
    max_retries=MAX_RETRIES,
    timeout=None,
    fuzz=FUZZ,
    log_errors=True,
    retryable_exceptions=None,
):
  """A function decorator that will retry when exceptions are thrown.

  Args:
    poll_interval: The time between tries in seconds. This is the maximum poll
      interval when fuzz is specified.
    max_retries: The maximum number of retries before giving up. If -1, this
      means continue until the timeout is reached. The function will stop
      retrying when either max_retries is met or timeout is reached.
    timeout: The timeout for all tries in seconds. If -1, this means continue
      until max_retries is met. The function will stop retrying when either
      max_retries is met or timeout is reached.
    fuzz: The amount of randomness in the sleep time. This is used to keep
      threads from all retrying at the same time. At 0, this means sleep exactly
      poll_interval seconds. At 1, this means sleep anywhere from 0 to
      poll_interval seconds.
    log_errors: A boolean describing whether errors should be logged.
    retryable_exceptions: A tuple of exceptions that should be retried. By
      default, this is None, which indicates that all exceptions should be
      retried.

  Returns:
    A function that wraps functions in retry logic. It can be
        used as a decorator.

  Raises:
    TimeoutExceededRetryError - if the provided (or default) timeout is exceeded
      while retrying the wrapped function.
    RetriesExceededRetryError - if the provided (or default) limit on the number
      of retry attempts is exceeded while retrying the wrapped function.
  """
  if retryable_exceptions is None:
    # TODO(user) Make retries less aggressive.
    retryable_exceptions = Exception

  def Wrap(f):
    """Wraps the supplied function with retry logic."""

    def WrappedFunction(*args, **kwargs):
      """Holds the retry logic."""
      local_timeout = FLAGS.default_timeout if timeout is None else timeout

      if local_timeout >= 0:
        deadline = time.time() + local_timeout
      else:
        deadline = float('inf')

      tries = 0
      while True:
        try:
          tries += 1
          return f(*args, **kwargs)
        except retryable_exceptions as e:
          fuzz_multiplier = 1 - fuzz + random.random() * fuzz
          sleep_time = poll_interval * fuzz_multiplier
          if (time.time() + sleep_time) >= deadline:
            raise TimeoutExceededRetryError() from e
          elif max_retries >= 0 and tries > max_retries:
            raise RetriesExceededRetryError() from e
          else:
            if log_errors:
              logging.info(
                  'Retrying exception running %s: %s', f.__qualname__, repr(e)
              )
            time.sleep(sleep_time)

    return WrappedFunction

  return Wrap


class _BoxedObject:
  """Box a value in a reference so it is modifiable inside an inner function.

  In python3 the nonlocal keyword could be used instead - but for python2
  there is no support for modifying an external scoped variable value.
  """

  def __init__(self, initial_value):
    self.value = initial_value


def _ReadIssueCommandOutput(tf_out, tf_err):
  """Reads IssueCommand Output from stdout and stderr."""
  tf_out.seek(0)
  stdout = tf_out.read().decode('ascii', 'ignore')
  tf_err.seek(0)
  stderr = tf_err.read().decode('ascii', 'ignore')
  return stdout, stderr


def IssueCommand(
    cmd: Iterable[str],
    env: Dict[str, str] | None = None,
    timeout: int | None = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    should_pre_log: bool = True,
    raise_on_failure: bool = True,
    suppress_failure: Callable[[str, str, int], bool] | None = None,
    suppress_logging: bool = False,
    raise_on_timeout: bool = True,
    stack_level: int = 1,
) -> Tuple[str, str, int]:
  """Tries running the provided command once.

  Args:
    cmd: A list of strings such as is given to the subprocess.Popen()
      constructor.
    env: A dict of key/value strings, such as is given to the subprocess.Popen()
      constructor, that contains environment variables to be injected.
    timeout: Timeout for the command in seconds. If the command has not finished
      before the timeout is reached, it will be killed. Set timeout to None to
      let the command run indefinitely. If the subprocess is killed, the return
      code will indicate an error, and stdout and stderr will contain what had
      already been written to them before the process was killed.
    cwd: Directory in which to execute the command.
    should_pre_log: A boolean indicating if command should be outputted alone
      prior to the output with command, stdout, & stderr. Useful for e.g. timing
      command length & standing out in logs.
    raise_on_failure: A boolean indicating if non-zero return codes should raise
      IssueCommandError.
    suppress_failure: A function passed (stdout, stderr, ret_code) for non-zero
      return codes to determine if the failure should be suppressed e.g. a
      delete command which fails because the item to be deleted does not exist.
    suppress_logging: A boolean indicated whether STDOUT and STDERR should be
      suppressed. Used for sensitive information.
    raise_on_timeout: A boolean indicating if killing the process due to the
      timeout being hit should raise a IssueCommandTimeoutError
    stack_level: Number of stack frames to skip & get an "interesting" caller,
      for logging. 1 skips this function, 2 skips this & its caller, etc..

  Returns:
    A tuple of stdout, stderr, and retcode from running the provided command.

  Raises:
    IssueCommandError: When raise_on_failure=True and retcode is non-zero.
    IssueCommandTimeoutError:  When raise_on_timeout=True and
                               command duration exceeds timeout
    ValueError: When incorrect parameters are passed in.
  """
  stack_level += 1
  if env:
    logger.debug('Environment variables: %s', env, stacklevel=stack_level)

  # Force conversion to string so you get a nice log statement before hitting a
  # type error or NPE.
  if isinstance(cmd, str):
    raise ValueError(
        f'Command must be a list of strings, but string {cmd} was received'
    )
  full_cmd = ' '.join(str(w) for w in cmd)
  if '; ' in full_cmd:
    logger.warning(
        (
            'Semicolon ; detected in command. Prefer && for better error '
            'handling. Feel free to ignore if not using semicolon to split '
            'commands. Full command: %s'
        ),
        full_cmd,
    )

  time_file_path = '/usr/bin/time'

  running_on_windows = RunningOnWindows()
  running_on_darwin = RunningOnDarwin()
  should_time = (
      not (running_on_windows or running_on_darwin)
      and os.path.isfile(time_file_path)
      and FLAGS.time_commands
  )
  shell_value = running_on_windows
  with (
      tempfile.TemporaryFile() as tf_out,
      tempfile.TemporaryFile() as tf_err,
      tempfile.NamedTemporaryFile(mode='r') as tf_timing,
  ):
    cmd_to_use = cmd
    if should_time:
      cmd_to_use = [
          time_file_path,
          '-o',
          tf_timing.name,
          '--quiet',
          '-f',
          ',  WallTime:%Es,  CPU:%Us,  MaxMemory:%Mkb ',
      ] + list(cmd)

    if should_pre_log:
      logger.info('Running: %s', full_cmd, stacklevel=stack_level)
    try:
      process = subprocess.Popen(
          cmd_to_use,
          env=env,
          shell=shell_value,
          stdin=subprocess.PIPE,
          stdout=tf_out,
          stderr=tf_err,
          cwd=cwd,
      )
    except TypeError as e:
      # Only perform this validation after a type error, in case we are being
      # too strict.
      non_strings = [s for s in cmd if not isinstance(s, str)]
      if non_strings:
        raise ValueError(
            f'Command {cmd} contains non-string elements {non_strings}.'
        ) from e
      raise

    did_timeout = _BoxedObject(False)
    was_killed = _BoxedObject(False)

    def _KillProcess():
      did_timeout.value = True
      if not raise_on_timeout:
        logger.warning(
            'IssueCommand timed out after %d seconds. Killing command "%s".',
            timeout,
            full_cmd,
            stacklevel=stack_level,
        )
      process.kill()
      was_killed.value = True

    timer = threading.Timer(timeout, _KillProcess)
    timer.start()

    try:
      process.wait()
    finally:
      timer.cancel()

    stdout, stderr = _ReadIssueCommandOutput(tf_out, tf_err)

    timing_output = ''
    if should_time:
      timing_output = tf_timing.read().rstrip('\n')

  logged_stdout = '[REDACTED]' if suppress_logging else stdout
  logged_stderr = '[REDACTED]' if suppress_logging else stderr
  debug_text = 'Ran: {%s}\nReturnCode:%s%s\nSTDOUT: %s\nSTDERR: %s' % (
      full_cmd,
      process.returncode,
      timing_output,
      logged_stdout,
      logged_stderr,
  )
  if _VM_COMMAND_LOG_MODE.value == VmCommandLogMode.ALWAYS_LOG or (
      _VM_COMMAND_LOG_MODE.value == VmCommandLogMode.LOG_ON_ERROR
      and process.returncode
  ):
    logger.info(debug_text, stacklevel=stack_level)

  # Raise timeout error regardless of raise_on_failure - as the intended
  # semantics is to ignore expected errors caused by invoking the command
  # not errors from PKB infrastructure.
  if did_timeout.value and raise_on_timeout:
    debug_text = (
        '{}\nIssueCommand timed out after {} seconds.  '
        '{} by perfkitbenchmarker.'.format(
            debug_text,
            timeout,
            'Process was killed'
            if was_killed.value
            else 'Process may have been killed',
        )
    )
    raise errors.VmUtil.IssueCommandTimeoutError(debug_text)
  elif process.returncode and (raise_on_failure or suppress_failure):
    if suppress_failure and suppress_failure(
        stdout, stderr, process.returncode
    ):
      # failure is suppressible, rewrite the stderr and return code as passing
      # since some callers assume either is a failure e.g.
      # perfkitbenchmarker.providers.aws.util.IssueRetryableCommand()
      return stdout, '', 0
    raise errors.VmUtil.IssueCommandError(debug_text)

  return stdout, stderr, process.returncode


def IssueBackgroundCommand(cmd, stdout_path, stderr_path, env=None):
  """Run the provided command once in the background.

  Args:
    cmd: Command to be run, as expected by subprocess.Popen.
    stdout_path: Redirect stdout here. Overwritten.
    stderr_path: Redirect stderr here. Overwritten.
    env: A dict of key/value strings, such as is given to the subprocess.Popen()
      constructor, that contains environment variables to be injected.
  """
  logging.debug('Environment variables: %s', env)

  full_cmd = ' '.join(cmd)
  logging.info('Spawning: %s', full_cmd)
  outfile = open(stdout_path, 'w')
  errfile = open(stderr_path, 'w')
  shell_value = RunningOnWindows()
  subprocess.Popen(
      cmd,
      env=env,
      shell=shell_value,
      stdout=outfile,
      stderr=errfile,
      close_fds=True,
  )


@Retry()
def IssueRetryableCommand(cmd, env=None, **kwargs):
  """Tries running the provided command until it succeeds or times out.

  Args:
    cmd: A list of strings such as is given to the subprocess.Popen()
      constructor.
    env: An alternate environment to pass to the Popen command.
    **kwargs: additional arguments for the command

  Returns:
    A tuple of stdout and stderr from running the provided command.
  """
  # Additional retries will break stack_level, but works for the first one.
  kwargs['stack_level'] = kwargs.get('stack_level', 1) + 2
  stdout, stderr, retcode = IssueCommand(
      cmd, env=env, raise_on_failure=False, **kwargs
  )
  if retcode:
    debug_text = 'Ran: {%s}\nReturnCode:%s\nSTDOUT: %s\nSTDERR: %s' % (
        ' '.join(cmd),
        retcode,
        stdout,
        stderr,
    )
    raise errors.VmUtil.CalledProcessException(
        'Command returned a non-zero exit code:\n{}'.format(debug_text)
    )
  return stdout, stderr


def ParseTimeCommandResult(command_result):
  """Parse command result and get time elapsed.

  Note this parses the output of bash's time builtin, not /usr/bin/time or other
  implementations. You may need to run something like bash -c "time ./command"
  to produce parseable output.

  Args:
     command_result: The result after executing a remote time command.

  Returns:
     Time taken for the command.
  """
  time_data = re.findall(r'real\s+(\d+)m(\d+.\d+)', command_result)
  time_in_seconds = 60 * float(time_data[0][0]) + float(time_data[0][1])
  return time_in_seconds


def ShouldRunOnExternalIpAddress(ip_type=None):
  """Returns whether a test should be run on an instance's external IP."""
  ip_type_to_check = ip_type or FLAGS.ip_addresses
  return ip_type_to_check in (
      IpAddressSubset.EXTERNAL,
      IpAddressSubset.BOTH,
      IpAddressSubset.REACHABLE,
  )


def ShouldRunOnInternalIpAddress(sending_vm, receiving_vm, ip_type=None):
  """Returns whether a test should be run on an instance's internal IP.

  Based on the command line flag --ip_addresses. Internal IP addresses are used
  when:

  * --ip_addresses=BOTH or --ip-addresses=INTERNAL
  * --ip_addresses=REACHABLE and 'sending_vm' can ping 'receiving_vm' on its
    internal IP.

  Args:
    sending_vm: VirtualMachine. The client.
    receiving_vm: VirtualMachine. The server.
    ip_type: optional ip_type to use instead of what is set in the FLAGS

  Returns:
    Whether a test should be run on an instance's internal IP.
  """
  ip_type_to_check = ip_type or FLAGS.ip_addresses
  return ip_type_to_check in (
      IpAddressSubset.BOTH,
      IpAddressSubset.INTERNAL,
  ) or (
      ip_type_to_check == IpAddressSubset.REACHABLE
      and sending_vm.IsReachable(receiving_vm)
  )


def GetLastRunUri():
  """Returns the last run_uri used (or None if it can't be determined)."""
  runs_dir_path = temp_dir.GetAllRunsDirPath()
  try:
    dir_names = next(os.walk(runs_dir_path))[1]
  except StopIteration:
    # The runs directory was not found.
    return None

  if not dir_names:
    # No run subdirectories were found in the runs directory.
    return None

  # Return the subdirectory with the most recent modification time.
  return max(
      dir_names, key=lambda d: os.path.getmtime(os.path.join(runs_dir_path, d))
  )


@contextlib.contextmanager
def NamedTemporaryFile(
    mode='w+b', prefix='tmp', suffix='', dir=None, delete=True
):
  """Behaves like tempfile.NamedTemporaryFile.

  The existing tempfile.NamedTemporaryFile has the annoying property on
  Windows that it cannot be opened a second time while it is already open.
  This makes it impossible to use it with a "with" statement in a cross platform
  compatible way. This serves a similar role, but allows the file to be closed
  within a "with" statement without causing the file to be unlinked until the
  context exits.

  Args:
    mode: see mode in tempfile.NamedTemporaryFile.
    prefix: see prefix in tempfile.NamedTemporaryFile.
    suffix: see suffix in tempfile.NamedTemporaryFile.
    dir: see dir in tempfile.NamedTemporaryFile.
    delete: see delete in NamedTemporaryFile.

  Yields:
    A cross platform file-like object which is "with" compatible.
  """
  f = tempfile.NamedTemporaryFile(
      mode=mode, prefix=prefix, suffix=suffix, dir=dir, delete=False
  )
  try:
    yield f
  finally:
    if not f.closed:
      f.close()
    if delete:
      os.unlink(f.name)


def GenerateSSHConfig(vms, vm_groups):
  """Generates an SSH config file to simplify connecting to the specified VMs.

  Writes a file to GetTempDir()/ssh_config with an SSH configuration for each VM
  provided in the arguments. Users can then SSH with any of the following:

      ssh -F <ssh_config_path> <vm_name>
      ssh -F <ssh_config_path> vm<vm_index>
      ssh -F <ssh_config_path> <group_name>-<index>

  Args:
    vms: list of BaseVirtualMachines.
    vm_groups: dict mapping VM group name string to list of BaseVirtualMachines.
  """
  target_file = os.path.join(GetTempDir(), 'ssh_config')
  template_path = data.ResourcePath('ssh_config.j2')
  environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
  with open(template_path) as fp:
    template = environment.from_string(fp.read())
  with open(target_file, 'w') as ofp:
    ofp.write(template.render({'vms': vms, 'vm_groups': vm_groups}))

  ssh_options = [
      '  ssh -F {} {}'.format(target_file, pattern)
      for pattern in ('<vm_name>', 'vm<index>', '<group_name>-<index>')
  ]
  logging.info(
      'ssh to VMs in this benchmark by name with:\n%s', '\n'.join(ssh_options)
  )


def RunningOnWindows():
  """Returns True if PKB is running on Windows."""
  return os.name == WINDOWS


def RunningOnDarwin():
  """Returns True if PKB is running on a Darwin OS machine."""
  return os.name != WINDOWS and platform.system() == DARWIN


def ExecutableOnPath(executable_name):
  """Return True if the given executable can be found on the path."""
  cmd = ['where'] if RunningOnWindows() else ['which']
  cmd.append(executable_name)

  shell_value = RunningOnWindows()
  process = subprocess.Popen(
      cmd, shell=shell_value, stdout=subprocess.PIPE, stderr=subprocess.PIPE
  )
  process.communicate()

  if process.returncode:
    return False
  return True


def GenerateRandomWindowsPassword(
    password_length=PASSWORD_LENGTH, special_chars='*!@#$+'
):
  """Generates a password that meets Windows complexity requirements."""
  # The special characters have to be recognized by the Azure CLI as
  # special characters. This greatly limits the set of characters
  # that we can safely use. See
  # https://github.com/Azure/azure-xplat-cli/blob/master/lib/commands/arm/vm/vmOsProfile._js#L145
  # Ensure that the password contains at least one of each 4 required
  # character types starting with letters to avoid starting with chars which
  # are problematic on the command line e.g. @.
  prefix = [
      random.choice(string.ascii_lowercase),
      random.choice(string.ascii_uppercase),
      random.choice(string.digits),
      random.choice(special_chars),
  ]
  password = [
      random.choice(string.ascii_letters + string.digits + special_chars)
      for _ in range(password_length - 4)
  ]
  return ''.join(prefix + password)


def CopyFileBetweenVms(filename, src_vm, src_path, dest_vm, dest_path):
  """Copies a file from the src_vm to the dest_vm."""
  with tempfile.NamedTemporaryFile() as tf:
    temp_path = tf.name
    src_vm.RemoteCopy(
        temp_path, os.path.join(src_path, filename), copy_to=False
    )
    dest_vm.RemoteCopy(
        temp_path, os.path.join(dest_path, filename), copy_to=True
    )


def ReplaceText(vm, current_value, new_value, file_name, regex_char='/'):
  """Replaces text <current_value> with <new_value> in remote <file_name>."""
  vm.RemoteCommand(
      'sed -i -r "s{regex_char}{current_value}{regex_char}'
      '{new_value}{regex_char}" {file}'.format(
          regex_char=regex_char,
          current_value=current_value,
          new_value=new_value,
          file=file_name,
      )
  )


def DictionaryToEnvString(dictionary, joiner=' '):
  """Convert a dictionary to a space sperated 'key=value' string.

  Args:
    dictionary: the key-value dictionary to be convert
    joiner: string to separate the entries in the returned value.

  Returns:
    a string representing the dictionary
  """
  return joiner.join(
      f'{key}={value}' for key, value in sorted(dictionary.items())
  )


def RenderTemplate(
    template_path,
    context,
    should_log_file: bool = False,
    trim_spaces: bool = False,
) -> str:
  """Renders a local Jinja2 template and returns its file name.

  The template will be provided variables defined in 'context'.

  Args:
    template_path: string. Local path to jinja2 template.
    context: dict. Variables to pass to the Jinja2 template during rendering.
    should_log_file: bool. Whether to log the file after rendering.
    trim_spaces: bool. Value for both trim_blocks and lstrip_blocks.

  Raises:
    jinja2.UndefinedError: if template contains variables not present in
      'context'.

  Returns:
    The name of the temporary file containing the rendered template.
  """
  with open(template_path) as fp:
    template_contents = fp.read()
  environment = jinja2.Environment(
      undefined=jinja2.StrictUndefined,
      trim_blocks=trim_spaces,
      lstrip_blocks=trim_spaces,
  )
  template = environment.from_string(template_contents)
  prefix = 'pkb-' + os.path.basename(template_path)
  with NamedTemporaryFile(
      prefix=prefix, dir=GetTempDir(), delete=False, mode='w'
  ) as tf:
    rendered_template = template.render(**context)
    if should_log_file:
      logging.info(
          'Rendered template from %s to %s with full text:\n%s',
          template_path,
          tf.name,
          rendered_template,
          stacklevel=2,
      )
    tf.write(rendered_template)
    tf.close()
    return tf.name


def CreateRemoteFile(vm, file_contents, file_path):
  """Creates a file on the remote server."""
  with NamedTemporaryFile(mode='w') as tf:
    tf.write(file_contents)
    tf.close()
    parent_dir = posixpath.dirname(file_path)
    vm.RemoteCommand(f'[ -d {parent_dir} ] || mkdir -p {parent_dir}')
    vm.PushFile(tf.name, file_path)


def ReadLocalFile(filename: str) -> str:
  """Read the local file."""
  file_path = posixpath.join(GetTempDir(), filename)
  stdout, _, _ = IssueCommand(['cat', file_path])
  return stdout
