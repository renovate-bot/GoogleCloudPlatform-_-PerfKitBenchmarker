# Copyright 2018 PerfKitBenchmarker Authors. All rights reserved.
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
"""Tests for providers.kubernetes.kubernetes_virtual_machine."""

import builtins
import contextlib
import json
import unittest
from absl import flags as flgs
from absl.testing import flagsaver
import mock
from perfkitbenchmarker import errors
from perfkitbenchmarker import os_types
from perfkitbenchmarker import provider_info
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.azure import util as azure_util
from perfkitbenchmarker.resources.kubernetes import kubernetes_pod_spec
from perfkitbenchmarker.resources.kubernetes import kubernetes_virtual_machine
from tests import pkb_common_test_case

FLAGS = flgs.FLAGS
FLAGS.kubernetes_anti_affinity = False

_COMPONENT = 'test_component'
_RUN_URI = 'fake_run_uri'
_NAME = 'fake_name'
_KUBECTL = 'fake_kubectl_path'
_KUBECONFIG = 'fake_kubeconfig_path'

_EXPECTED_CALL_BODY_WITHOUT_GPUS = """
{
    "spec": {
        "dnsPolicy":
            "ClusterFirst",
        "volumes": [],
        "containers": [{
            "name": "fake_name",
            "workingDir": "/root",
            "volumeMounts": [],
            "image": "test_image",
            "securityContext": {
                "privileged": true
            },
            "command": ["tail", "-f", "/dev/null"]
        }],
        "tolerations": [{
            "key": "kubernetes.io/arch",
            "operator": "Exists",
            "effect": "NoSchedule"
        }]
    },
    "kind": "Pod",
    "metadata": {
        "name": "fake_name",
        "labels": {
            "pkb": "fake_name"
        }
    },
    "apiVersion": "v1"
}
"""

_EXPECTED_CALL_BODY_WITH_2_GPUS = """
{
    "spec": {
        "dnsPolicy":
            "ClusterFirst",
        "volumes": [],
        "containers": [{
            "name": "fake_name",
            "volumeMounts": [],
            "workingDir": "/root",
            "image": "test_image",
            "securityContext": {
                "privileged": true
            },
            "resources" : {
              "limits": {
                "nvidia.com/gpu": "2"
                },
              "requests": {
                "nvidia.com/gpu": "2"
                }
            },
            "command": ["tail", "-f", "/dev/null"]
        }],
        "tolerations": [{
            "key": "kubernetes.io/arch",
            "operator": "Exists",
            "effect": "NoSchedule"
        }]
    },
    "kind": "Pod",
    "metadata": {
        "name": "fake_name",
        "labels": {
            "pkb": "fake_name"
        }
    },
    "apiVersion": "v1"
}
"""

_EXPECTED_CALL_BODY_UBUNTU_2404 = """
{
    "spec": {
        "dnsPolicy":
            "ClusterFirst",
        "volumes": [],
        "containers": [{
            "name": "fake_name",
            "volumeMounts": [],
            "workingDir": "/root",
            "image": "ubuntu:24.04",
            "securityContext": {
                "privileged": true
            },
            "command": ["tail", "-f", "/dev/null"]
        }],
        "tolerations": [{
            "key": "kubernetes.io/arch",
            "operator": "Exists",
            "effect": "NoSchedule"
        }]
    },
    "kind": "Pod",
    "metadata": {
        "name": "fake_name",
        "labels": {
            "pkb": "fake_name"
        }
    },
    "apiVersion": "v1"
}
"""

_EXPECTED_CALL_BODY_WITH_VM_GROUP = """
{
    "spec": {
        "dnsPolicy":
            "ClusterFirst",
        "volumes": [],
        "containers": [{
            "name": "fake_name",
            "volumeMounts": [],
            "workingDir": "/root",
            "image": "test_image",
            "securityContext": {
                "privileged": true
            },
            "command": ["tail", "-f", "/dev/null"]
        }],
        "tolerations": [{
            "key": "kubernetes.io/arch",
            "operator": "Exists",
            "effect": "NoSchedule"
        }],
        "nodeSelector": {
            "pkb_nodepool": "myvmgroup"
        }
    },
    "kind": "Pod",
    "metadata": {
        "name": "fake_name",
        "labels": {
            "pkb": "fake_name"
        }
    },
    "apiVersion": "v1"
}
"""

_EXPECTED_CALL_BODY_WITH_HOST_NETWORK = """
{
    "spec": {
        "hostNetwork": true,
        "dnsPolicy":
            "ClusterFirstWithHostNet",
        "volumes": [],
        "containers": [{
            "name": "fake_name",
            "workingDir": "/root",
            "volumeMounts": [],
            "image": "test_image",
            "securityContext": {
                "privileged": true
            },
            "command": ["tail", "-f", "/dev/null"]
        }],
        "tolerations": [{
            "key": "kubernetes.io/arch",
            "operator": "Exists",
            "effect": "NoSchedule"
        }]
    },
    "kind": "Pod",
    "metadata": {
        "name": "fake_name",
        "labels": {
            "pkb": "fake_name"
        }
    },
    "apiVersion": "v1"
}
"""


def get_write_mock_from_temp_file_mock(temp_file_mock):
  """Returns the write method mock from the NamedTemporaryFile mock.

  This can be used to make assertions about the calls make to write(),
  which exists on the instance returned from the NamedTemporaryFile mock.

  The reason for the __enter__() in this context is due to the fact
  that NamedTemporaryFile is used in a context manager inside
  kubernetes_helper.py.

  Args:
   temp_file_mock: mock object of the NamedTemporaryFile() contextManager
  """
  return temp_file_mock().__enter__().write


@contextlib.contextmanager
def patch_critical_objects(stdout='', stderr='', return_code=0, flags=FLAGS):
  with contextlib.ExitStack() as stack:
    retval = (stdout, stderr, return_code)

    flags.gcloud_path = 'gcloud'
    flags.run_uri = _RUN_URI
    flags.kubectl = _KUBECTL
    flags.kubeconfig = _KUBECONFIG

    stack.enter_context(mock.patch(builtins.__name__ + '.open'))
    stack.enter_context(mock.patch(vm_util.__name__ + '.PrependTempDir'))

    # Save and return the temp_file mock here so that we can access the write()
    # call on the instance that the mock returned. This allows us to verify
    # that the body of the file is what we expect it to be (useful for
    # verifying that the pod.yml body was written correctly).
    temp_file = stack.enter_context(
        mock.patch(vm_util.__name__ + '.NamedTemporaryFile')
    )

    issue_command = stack.enter_context(
        mock.patch(
            vm_util.__name__ + '.IssueCommand',
            return_value=retval,
            autospec=True,
        )
    )

    yield issue_command, temp_file


class TestKubernetesVirtualMachine(
    kubernetes_virtual_machine.KubernetesVirtualMachine,
    pkb_common_test_case.TestLinuxVirtualMachine,
):
  pass


class BaseKubernetesVirtualMachineTestCase(
    pkb_common_test_case.PkbCommonTestCase
):

  def assertJsonEqual(self, str1, str2):
    json1 = json.loads(str1)
    json2 = json.loads(str2)
    self.assertEqual(
        json.dumps(json1, sort_keys=True), json.dumps(json2, sort_keys=True)
    )


class KubernetesResourcesTestCase(BaseKubernetesVirtualMachineTestCase):

  @staticmethod
  def create_virtual_machine_spec():
    spec = kubernetes_pod_spec.KubernetesPodSpec(
        _COMPONENT,
        resource_limits={'cpus': 2, 'memory': '5GiB'},
        resource_requests={'cpus': 1.5, 'memory': '4GiB'},
        gpu_count=2,
        gpu_type='k80',
    )
    return spec

  def testPodResourceLimits(self):
    spec = self.create_virtual_machine_spec()
    assert spec.resource_limits is not None
    self.assertEqual(spec.resource_limits.cpus, 2)
    self.assertEqual(spec.resource_limits.memory, 5120)

  def testCreatePodResourceBody(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects():
      kub_vm = TestKubernetesVirtualMachine(spec)

      expected = {
          'limits': {'cpu': '2', 'memory': '5120Mi', 'nvidia.com/gpu': '2'},
          'requests': {'cpu': '1.5', 'memory': '4096Mi', 'nvidia.com/gpu': '2'},
      }
      actual = kub_vm._BuildResourceBody()
      self.assertDictEqual(expected, actual)

  def testGetMetadata(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects():
      kub_vm = TestKubernetesVirtualMachine(spec)
      subset_of_expected_metadata = {
          'pod_cpu_limit': 2,
          'pod_memory_limit_mb': 5120,
          'pod_cpu_request': 1.5,
          'pod_memory_request_mb': 4096,
      }
      actual = kub_vm.GetResourceMetadata()
      self.assertEqual(actual, {**actual, **subset_of_expected_metadata})

  def testAnnotations(self):
    spec = self.create_virtual_machine_spec()
    FLAGS.k8s_sriov_network = 'sriov-vlan50'
    with patch_critical_objects():
      kub_vm = TestKubernetesVirtualMachine(spec)
      subset_of_expected_metadata = {
          'annotations': {'sriov_network': 'sriov-vlan50'}
      }
      actual = kub_vm.GetResourceMetadata()
      self.assertEqual(actual, {**actual, **subset_of_expected_metadata})


class KubernetesVirtualMachineOsTypesTestCase(
    BaseKubernetesVirtualMachineTestCase
):

  @staticmethod
  def create_kubernetes_vm(os_type):
    spec = kubernetes_pod_spec.KubernetesPodSpec(_COMPONENT)
    vm_class = virtual_machine.GetVmClass(
        provider_info.GCP, os_type, provider_info.KUBERNETES
    )
    kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
    kub_vm._WaitForPodBootCompletion = lambda: None
    kub_vm._Create()

  def testCreateUbuntu2404(self):
    with patch_critical_objects() as (_, temp_file):
      self.create_kubernetes_vm(os_types.UBUNTU2404)

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      create_json = json.loads(write_mock.call_args[0][0])
      self.assertEqual(
          create_json['spec']['containers'][0]['image'], 'ubuntu:24.04'
      )


class KubernetesVirtualMachineClassFoundTestCase(
    BaseKubernetesVirtualMachineTestCase
):

  def testClassNotKubernetesCloud(self):
    with self.assertRaises(errors.Resource.SubclassNotFoundError):
      virtual_machine.GetVmClass(provider_info.KUBERNETES, os_types.UBUNTU2404)

  @flagsaver.flagsaver(
      (provider_info.VM_PLATFORM, provider_info.KUBERNETES_PLATFORM)
  )
  def testClassFoundByKubernetesPlatform(self):
    FLAGS.cloud = provider_info.AWS
    spec = kubernetes_pod_spec.KubernetesPodSpec(_COMPONENT)
    vm_class = virtual_machine.GetVmClass(
        provider_info.GCP, os_types.UBUNTU2404
    )
    kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
    self.assertIsInstance(
        kub_vm,
        kubernetes_virtual_machine.Ubuntu2404BasedKubernetesVirtualMachine,
    )
    self.assertEqual(kub_vm.CLOUD, provider_info.GCP)
    self.assertEqual(kub_vm.PLATFORM, provider_info.KUBERNETES_PLATFORM)
    self.assertEqual(kub_vm.cloud, provider_info.GCP)

  @flagsaver.flagsaver(
      (provider_info.VM_PLATFORM, provider_info.KUBERNETES_PLATFORM)
  )
  def test2004AzureClassFoundByKubernetesPlatform(self):
    FLAGS.cloud = None
    spec = kubernetes_pod_spec.KubernetesPodSpec(_COMPONENT)
    vm_class = virtual_machine.GetVmClass(
        provider_info.AZURE, os_types.UBUNTU2004
    )
    kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
    self.assertIsInstance(
        kub_vm,
        kubernetes_virtual_machine.Ubuntu2004BasedKubernetesVirtualMachine,
    )
    self.assertEqual(kub_vm.cloud, provider_info.AZURE)


class KubernetesVirtualMachineVmGroupAffinityTestCase(
    BaseKubernetesVirtualMachineTestCase
):

  @staticmethod
  def create_kubernetes_vm():
    spec = kubernetes_pod_spec.KubernetesPodSpec(
        _COMPONENT,
        image='test_image',
        install_packages=False,
        machine_type='test_machine_type',
        zone='test_zone',
    )
    kub_vm = TestKubernetesVirtualMachine(spec)
    kub_vm.name = _NAME
    kub_vm.vm_group = 'my_vm_group'
    kub_vm._WaitForPodBootCompletion = lambda: None  # pylint: disable=invalid-name
    kub_vm._Create()

  def testCreateVmGroupAffinity(self):
    with patch_critical_objects() as (_, temp_file):
      self.create_kubernetes_vm()

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      self.assertJsonEqual(
          write_mock.call_args[0][0], _EXPECTED_CALL_BODY_WITH_VM_GROUP
      )


class KubernetesVirtualMachineTestCase(BaseKubernetesVirtualMachineTestCase):

  @staticmethod
  def create_virtual_machine_spec():
    spec = kubernetes_pod_spec.KubernetesPodSpec(
        _COMPONENT,
        image='test_image',
        install_packages=False,
        machine_type='test_machine_type',
        zone='test_zone',
    )
    return spec

  def testCreate(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects() as (issue_command, _):
      kub_vm = TestKubernetesVirtualMachine(spec)
      kub_vm._WaitForPodBootCompletion = lambda: None  # pylint: disable=invalid-name
      kub_vm._Create()
      command = issue_command.call_args[0][0]
      command_string = ' '.join(command[:4])

      self.assertEqual(issue_command.call_count, 1)
      self.assertIn(
          '{} --kubeconfig={} create -f'.format(_KUBECTL, _KUBECONFIG),
          command_string,
      )

  def testCreatePodBodyWrittenCorrectly(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects() as (_, temp_file):
      kub_vm = TestKubernetesVirtualMachine(spec)
      # Need to set the name explicitly on the instance because the test
      # running is currently using a single PKB instance, so the BaseVm
      # instance counter is at an unpredictable number at this stage, and it is
      # used to set the name.
      kub_vm.name = _NAME
      kub_vm._WaitForPodBootCompletion = lambda: None
      kub_vm._Create()

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      self.assertJsonEqual(
          write_mock.call_args[0][0], _EXPECTED_CALL_BODY_WITHOUT_GPUS
      )

  def testCreatePodBodyWithHostNetwork(self):
    spec = self.create_virtual_machine_spec()
    spec.host_network = True
    with patch_critical_objects() as (_, temp_file):
      kub_vm = TestKubernetesVirtualMachine(spec)
      # Need to set the name explicitly on the instance because the test
      # running is currently using a single PKB instance, so the BaseVm
      # instance counter is at an unpredictable number at this stage, and it is
      # used to set the name.
      kub_vm.name = _NAME
      kub_vm._WaitForPodBootCompletion = lambda: None
      kub_vm._Create()

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      self.assertJsonEqual(
          write_mock.call_args[0][0], _EXPECTED_CALL_BODY_WITH_HOST_NETWORK
      )

  def testDownloadPreprovisionedDataAws(self):
    spec = self.create_virtual_machine_spec()
    vm_class = virtual_machine.GetVmClass(
        provider_info.AWS, os_types.UBUNTU2404, provider_info.KUBERNETES
    )
    with patch_critical_objects(flags=FLAGS) as (issue_command, _):
      kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
      kub_vm.DownloadPreprovisionedData('path', 'name', 'filename')

      command = issue_command.call_args[0][0]
      command_string = ' '.join(command)
      self.assertIn('s3', command_string)

  def testDownloadPreprovisionedDataAzure(self):
    azure_util.GetAzureStorageConnectionString = mock.Mock(return_value='')
    spec = self.create_virtual_machine_spec()
    vm_class = virtual_machine.GetVmClass(
        provider_info.AZURE, os_types.UBUNTU2404, provider_info.KUBERNETES
    )
    with patch_critical_objects() as (issue_command, _):
      kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
      kub_vm.DownloadPreprovisionedData('path', 'name', 'filename')

      command = issue_command.call_args[0][0]
      command_string = ' '.join(command)
      self.assertIn('az storage blob download', command_string)
      self.assertIn('--connection-string', command_string)

  def testDownloadPreprovisionedDataGcp(self):
    vm_class = virtual_machine.GetVmClass(
        provider_info.GCP, os_types.UBUNTU2404, provider_info.KUBERNETES
    )
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects() as (issue_command, _):
      kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
      kub_vm.DownloadPreprovisionedData('path', 'name', 'filename')

      command = issue_command.call_args[0][0]
      command_string = ' '.join(command)
      self.assertIn('gcloud storage', command_string)

  def testIsAarch64(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects('aarch64'):
      kub_vm = TestKubernetesVirtualMachine(spec)
      self.assertTrue(kub_vm.is_aarch64)

    with patch_critical_objects('x86_64'):
      kub_vm = TestKubernetesVirtualMachine(spec)
      self.assertFalse(kub_vm.is_aarch64)


class KubernetesVirtualMachineWithGpusTestCase(
    BaseKubernetesVirtualMachineTestCase
):

  @staticmethod
  def create_virtual_machine_spec():
    spec = kubernetes_pod_spec.KubernetesPodSpec(
        _COMPONENT,
        image='test_image',
        gpu_count=2,
        gpu_type='k80',
        install_packages=False,
        machine_type='test_machine_type',
        zone='test_zone',
    )
    return spec

  def testCreate(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects() as (issue_command, _):
      kub_vm = TestKubernetesVirtualMachine(spec)
      kub_vm._WaitForPodBootCompletion = lambda: None
      kub_vm._Create()
      command = issue_command.call_args[0][0]
      command_string = ' '.join(command[:4])

      self.assertEqual(issue_command.call_count, 1)
      self.assertIn(
          '{} --kubeconfig={} create -f'.format(_KUBECTL, _KUBECONFIG),
          command_string,
      )

  def testCreatePodBodyWrittenCorrectly(self):
    spec = self.create_virtual_machine_spec()
    with patch_critical_objects() as (_, temp_file):
      kub_vm = TestKubernetesVirtualMachine(spec)
      # Need to set the name explicitly on the instance because the test
      # running is currently using a single PKB instance, so the BaseVm
      # instance counter is at an unpredictable number at this stage, and it is
      # used to set the name.
      kub_vm.name = _NAME
      kub_vm._WaitForPodBootCompletion = lambda: None
      kub_vm._Create()

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      self.assertJsonEqual(
          write_mock.call_args[0][0], _EXPECTED_CALL_BODY_WITH_2_GPUS
      )


class KubernetesVirtualMachine(BaseKubernetesVirtualMachineTestCase):

  @staticmethod
  def create_virtual_machine_spec():
    spec = kubernetes_pod_spec.KubernetesPodSpec(
        _COMPONENT,
        install_packages=False,
        machine_type='test_machine_type',
        zone='test_zone',
    )
    return spec

  def testCreatePodBodyWrittenCorrectly(self):
    spec = self.create_virtual_machine_spec()
    vm_class = virtual_machine.GetVmClass(
        provider_info.GCP, os_types.UBUNTU2404, provider_info.KUBERNETES
    )
    with patch_critical_objects() as (_, temp_file):
      kub_vm = vm_class(spec)  # pytype: disable=not-instantiable
      # Need to set the name explicitly on the instance because the test
      # running is currently using a single PKB instance, so the BaseVm
      # instance counter is at an unpredictable number at this stage, and it is
      # used to set the name.
      kub_vm.name = _NAME
      kub_vm._WaitForPodBootCompletion = lambda: None
      kub_vm._Create()

      write_mock = get_write_mock_from_temp_file_mock(temp_file)
      self.assertJsonEqual(
          write_mock.call_args[0][0], _EXPECTED_CALL_BODY_UBUNTU_2404
      )


if __name__ == '__main__':
  unittest.main()
