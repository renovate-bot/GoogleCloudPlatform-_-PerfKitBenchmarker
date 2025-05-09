#!/usr/bin/env python

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

"""Classes to collect and publish performance samples to various sinks."""


import abc
import collections
import copy
import csv
import datetime
import fcntl
import http.client as httplib
import itertools
import json
import logging
import math
import operator
import pprint
import sys
import time
from typing import Any
import uuid

from absl import flags
from perfkitbenchmarker import events
from perfkitbenchmarker import flag_util
from perfkitbenchmarker import log_util
from perfkitbenchmarker import sample as pkb_sample
from perfkitbenchmarker import version
from perfkitbenchmarker import vm_util
import pytz
import six
from six.moves import urllib

FLAGS = flags.FLAGS

flags.DEFINE_string(
    'product_name',
    'PerfKitBenchmarker',
    'The product name to use when publishing results.',
)

flags.DEFINE_boolean(
    'official',
    False,
    'A boolean indicating whether results are official or not. The '
    'default is False. Official test results are treated and queried '
    'differently from non-official test results.',
)

flags.DEFINE_boolean(
    'hostname_metadata',
    False,
    'A boolean indicating whether to publish VM hostnames as part of sample '
    'metadata.',
)

DEFAULT_JSON_OUTPUT_NAME = 'perfkitbenchmarker_results.json'
JSON_PATH = flags.DEFINE_string(
    'json_path',
    DEFAULT_JSON_OUTPUT_NAME,
    'A path to write newline-delimited JSON results. '
    'Default: write to a run-specific temporary directory. '
    "Passing '' skips local publishing.",
)
flags.DEFINE_enum(
    'json_write_mode',
    'w',
    ['w', 'a'],
    'Open mode for file specified by --json_path. Default: overwrite file',
)
flags.DEFINE_boolean(
    'collapse_labels', True, 'Collapse entries in labels in JSON output.'
)
flags.DEFINE_string('csv_path', None, 'A path to write CSV-format results')

flags.DEFINE_string(
    'bigquery_table',
    None,
    'The BigQuery table to publish results to. This should be of the form '
    '"[project_id:]dataset_name.table_name".',
)
flags.DEFINE_string('bq_path', 'bq', 'Path to the "bq" executable.')
flags.DEFINE_string(
    'bq_project', None, 'Project to use for authenticating with BigQuery.'
)
flags.DEFINE_string(
    'service_account', None, 'Service account to use to authenticate with BQ.'
)
flags.DEFINE_string(
    'service_account_private_key',
    None,
    'Service private key for authenticating with BQ.',
)
flags.DEFINE_string(
    'application_default_credential_file',
    None,
    'Application default credentials file for authenticating with BQ.',
)

flags.DEFINE_string('gsutil_path', 'gsutil', 'path to the "gsutil" executable')
flags.DEFINE_string(
    'cloud_storage_bucket',
    None,
    'GCS bucket to upload records to. Bucket must exist. '
    'This flag differs from --hourly_partitioned_cloud_storage_bucket '
    'by putting records directly in the bucket.',
)
PARTITIONED_GCS_URL = flags.DEFINE_string(
    'hourly_partitioned_cloud_storage_bucket',
    None,
    'GCS bucket to upload records to. Bucket must exist. This flag differs '
    'from --cloud_storage_bucket by putting records in subfolders based on '
    'time of publish. i.e. gs://bucket/YYYY/mm/dd/HH/data.',
)
flags.DEFINE_string(
    'es_uri',
    None,
    'The Elasticsearch address and port. e.g. http://localhost:9200',
)

flags.DEFINE_string(
    'es_index', 'perfkit', 'Elasticsearch index name to store documents'
)

flags.DEFINE_string('es_type', 'result', 'Elasticsearch document type')

flags.DEFINE_multi_string(
    'metadata',
    [],
    'A colon separated key-value pair that will be added to the labels field '
    'of all samples as metadata. Multiple key-value pairs may be specified '
    'by separating each pair by commas.',
)

_THROW_ON_METADATA_CONFLICT = flags.DEFINE_boolean(
    'throw_on_metadata_conflict',
    True,
    'Behavior when default metadata conflicts with existing metadata. If true, '
    'an error is raised. Otherwise, the existing metadata value is used.',
)

flags.DEFINE_string(
    'influx_uri',
    None,
    'The Influx DB address and port. Expects the format hostname:port'
    'If port is not passed in it assumes port 80. e.g. localhost:8086',
)

flags.DEFINE_string(
    'influx_db_name',
    'perfkit',
    'Name of Influx DB database that you wish to publish to or create',
)

flags.DEFINE_boolean(
    'record_log_publisher', True, 'Whether to use the log publisher or not.'
)

DEFAULT_CREDENTIALS_JSON = 'credentials.json'
GCS_OBJECT_NAME_LENGTH = 20

# A list of SamplePublishers that can be extended to add support for publishing
# types beyond those in this module. The classes should not require any
# arguments to their __init__ methods. The SampleCollector will unconditionally
# call PublishSamples using Publishers added via this method.
EXTERNAL_PUBLISHERS = []


# Used to publish samples in Pacific datetime
_PACIFIC_TZ = pytz.timezone('US/Pacific')

# metadata to list all entries instead of using a representative VM
_VM_METADATA_TO_LIST_PLURAL = {
    'id': 'ids',
    'name': 'names',
    'ip_address': 'ip_addresses',
}


def PublishRunStageSamples(benchmark_spec, samples):
  """Publishes benchmark run-stage samples immediately.

  Typically, a benchmark publishes samples by returning them from the Run
  function so that they can be pubished at set points (publish periods or at the
  end of a run). This function can be called to publish the samples immediately.

  Note that metadata for the run number will not be added to such samples.
  TODO(deitz): Can we still add the run number? This will require passing a run
  number or callback to the benchmark Run functions (or some other mechanism).

  Args:
    benchmark_spec: The BenchmarkSpec created for the benchmark.
    samples: A list of samples to publish.
  """
  events.benchmark_samples_created.send(
      benchmark_spec=benchmark_spec, samples=samples
  )
  collector = SampleCollector()
  collector.AddSamples(samples, benchmark_spec.name, benchmark_spec)
  collector.PublishSamples()


# GetLabelsFromDict() and SampleLabelsToDict() currently encode/decode the label
# dictionary in a way that does not support arbitrary strings as keys or values:
# specifically, certain combinations of the :/,/| characters can cause issues.
# The encoding might change in the future if more robustness is needed.


def GetLabelsFromDict(metadata: dict[Any, Any]) -> str:
  """Converts a metadata dictionary to a string of labels sorted by key.

  Args:
    metadata: a dictionary of string key value pairs.

  Returns:
    A string of labels, sorted by key, in the format that Perfkit uses.
  """
  labels = []
  for k, v in sorted(metadata.items()):
    labels.append('|%s:%s|' % (k, v))
  return ','.join(labels)


def LabelsToDict(labels_str: str) -> dict[str, str]:
  """Deserializes labels from string.

  Meant to invert GetLabelsFromDict().

  Args:
    labels_str: The string encoding the labels.

  Returns:
    A python dictionary mapping label names to contents.
  """
  # labels_str is of the form |k1:v1|,|k2:v2|.
  entries = labels_str[1:-1].split('|,|')
  split_entries = [s.split(':', 1) for s in entries]
  return {k: v for k, v in split_entries}


class MetadataProvider(metaclass=abc.ABCMeta):
  """A provider of sample metadata."""

  @abc.abstractmethod
  def AddMetadata(self, metadata, benchmark_spec) -> dict[str, Any]:
    """Add metadata to a dictionary.

    Args:
      metadata: dict. Dictionary of metadata to update.
      benchmark_spec: BenchmarkSpec. The benchmark specification.

    Returns:
      Updated 'metadata'.
    """
    raise NotImplementedError()


class DefaultMetadataProvider(MetadataProvider):
  """Adds default metadata to samples."""

  def AddMetadata(self, metadata: dict[str, Any], benchmark_spec):
    new_metadata = {}
    new_metadata['perfkitbenchmarker_version'] = version.VERSION
    if FLAGS.simulate_maintenance:
      new_metadata['simulate_maintenance'] = True
    if FLAGS.hostname_metadata:
      new_metadata['hostnames'] = ','.join(
          [vm.hostname for vm in benchmark_spec.vms]
      )
    if benchmark_spec.container_cluster:
      cluster = benchmark_spec.container_cluster
      for k, v in cluster.GetResourceMetadata().items():
        new_metadata['container_cluster_' + k] = v

    if benchmark_spec.relational_db:
      db = benchmark_spec.relational_db
      for k, v in db.GetResourceMetadata().items():
        # TODO(user): Rename to relational_db.
        new_metadata['managed_relational_db_' + k] = v

    if benchmark_spec.pinecone:
      pinecone = benchmark_spec.pinecone
      for k, v in pinecone.GetResourceMetadata().items():
        new_metadata['pinecone_' + k] = v

    if benchmark_spec.memory_store:
      memory_store = benchmark_spec.memory_store
      for k, v in memory_store.GetResourceMetadata().items():
        new_metadata[k] = v

    for name, tpu in benchmark_spec.tpu_groups.items():
      for k, v in tpu.GetResourceMetadata().items():
        new_metadata['tpu_' + k] = v

    for name, vms in benchmark_spec.vm_groups.items():
      if len(vms) == 0:
        continue

      # Get a representative VM so that we can publish the cloud, zone,
      # machine type, and image.
      vm = vms[-1]
      name_prefix = '' if name == 'default' else name + '_'
      for k, v in vm.GetResourceMetadata().items():
        if k not in _VM_METADATA_TO_LIST_PLURAL:
          new_metadata[name_prefix + k] = v
      new_metadata[name_prefix + 'vm_count'] = len(vms)
      for k, v in vm.GetOSResourceMetadata().items():
        new_metadata[name_prefix + k] = v

      if vm.scratch_disks:
        data_disk = vm.scratch_disks[0]
        new_metadata[name_prefix + 'data_disk_count'] = len(vm.scratch_disks)
        for key, value in data_disk.GetResourceMetadata().items():
          new_metadata[name_prefix + 'data_disk_0_%s' % (key,)] = value

    # Get some new_metadata from all VMs:
    # Here having a lack of a prefix indicate the union of all groups, where it
    # signaled the default vm group above.
    vm_groups_and_all = benchmark_spec.vm_groups | {None: benchmark_spec.vms}
    for group_name, vms in vm_groups_and_all.items():
      name_prefix = group_name + '_' if group_name else ''
      # since id and name are generic new_metadata prefix vm, so it is clear
      # what the resource is.
      name_prefix += 'vm_'
      for key, key_plural in _VM_METADATA_TO_LIST_PLURAL.items():
        values = []
        for vm in vms:
          if value := vm.GetResourceMetadata().get(key):
            values.append(value)
        if values:
          new_metadata[name_prefix + key_plural] = ','.join(values)

    if FLAGS.set_files:
      new_metadata['set_files'] = ','.join(FLAGS.set_files)
    if FLAGS.sysctl:
      new_metadata['sysctl'] = ','.join(FLAGS.sysctl)

    # Add new values, keeping old ones in conflicts.
    overlapping_keys = set(new_metadata.keys()).intersection(metadata.keys())
    if overlapping_keys and _THROW_ON_METADATA_CONFLICT.value:
      conflicts = []
      for key in overlapping_keys:
        if new_metadata[key] != metadata[key]:
          conflicts.append(f'{key}: {new_metadata[key]} != {metadata[key]}')
      if conflicts:
        raise ValueError(
            'Conflicting keys are already set in metadata & are being '
            'overwritten by default metadata provider. Resolve by using a '
            'different VM group, adding prefixes and/or different names to the '
            'metadata keys, or by setting --throw_on_metadata_conflict=False.'
            'Conflicts, with new value on left and old value on right: %s'
            % conflicts
        )
    metadata = new_metadata | metadata
    # Flatten all user metadata into a single list (since each string in the
    # FLAGS.metadata can actually be several key-value pairs) and then
    # iterate over it.
    parsed_metadata = flag_util.ParseKeyValuePairs(FLAGS.metadata)
    metadata.update(parsed_metadata)
    return metadata


DEFAULT_METADATA_PROVIDERS = [DefaultMetadataProvider()]


class SamplePublisher(metaclass=abc.ABCMeta):
  """An object that can publish performance samples."""

  # Time series data is long. Turn this flag off to hide time series data.
  PUBLISH_CONSOLE_LOG_DATA = True

  @abc.abstractmethod
  def PublishSamples(self, samples: list[pkb_sample.SampleDict]):
    """Publishes 'samples'.

    PublishSamples will be called exactly once. Calling
    SamplePublisher.PublishSamples multiple times may result in data being
    overwritten.

    Args:
      samples: list of dicts to publish.
    """
    raise NotImplementedError()


class CSVPublisher(SamplePublisher):
  """Publisher which writes results in CSV format to a specified path.

  The default field names are written first, followed by all unique metadata
  keys found in the data.
  """

  _DEFAULT_FIELDS = (
      'timestamp',
      'test',
      'metric',
      'value',
      'unit',
      'product_name',
      'official',
      'owner',
      'run_uri',
      'sample_uri',
  )

  def __init__(self, path):
    super().__init__()
    self._path = path

  def PublishSamples(self, samples):
    samples = list(samples)
    # Union of all metadata keys.
    meta_keys = sorted(
        # pylint: disable-next=g-complex-comprehension
        {key for sample in samples for key in sample['metadata']}
    )

    logging.info('Writing CSV results to %s', self._path)
    with open(self._path, 'w') as fp:
      writer = csv.DictWriter(fp, list(self._DEFAULT_FIELDS) + meta_keys)
      writer.writeheader()

      for sample in samples:
        d = {}
        d.update(sample)
        d.update(d.pop('metadata'))
        writer.writerow(d)


class PrettyPrintStreamPublisher(SamplePublisher):
  """Writes samples to an output stream, defaulting to stdout.

  Samples are pretty-printed and summarized. Example output (truncated):

    -------------------------PerfKitBenchmarker Results Summary--------------
    COREMARK:
      num_cpus="4"
      Coremark Score                    44145.237832
      End to End Runtime                  289.477677 seconds
    NETPERF:
      client_machine_type="n1-standard-4" client_zone="us-central1-a" ....
      TCP_RR_Transaction_Rate  1354.04 transactions_per_second (ip_type="ext ...
      TCP_RR_Transaction_Rate  3972.70 transactions_per_second (ip_type="int ...
      TCP_CRR_Transaction_Rate  449.69 transactions_per_second (ip_type="ext ...
      TCP_CRR_Transaction_Rate 1271.68 transactions_per_second (ip_type="int ...
      TCP_STREAM_Throughput    1171.04 Mbits/sec               (ip_type="ext ...
      TCP_STREAM_Throughput    6253.24 Mbits/sec               (ip_type="int ...
      UDP_RR_Transaction_Rate  1380.37 transactions_per_second (ip_type="ext ...
      UDP_RR_Transaction_Rate  4336.37 transactions_per_second (ip_type="int ...
      End to End Runtime        444.33 seconds

    -------------------------
    For all tests: cloud="GCP" image="ubuntu-14-04" machine_type="n1-standa ...

  Attributes:
    stream: File-like object. Output stream to print samples.
  """

  PUBLISH_CONSOLE_LOG_DATA = False

  def __init__(self, stream=None):
    super().__init__()
    self.stream = stream or sys.stdout

  def __repr__(self):
    return '<{} stream={}>'.format(type(self).__name__, self.stream)

  def _FindConstantMetadataKeys(self, samples):
    """Finds metadata keys which are constant across a collection of samples.

    Args:
      samples: List of dicts, as passed to SamplePublisher.PublishSamples.

    Returns:
      The set of metadata keys for which all samples in 'samples' have the same
      value.
    """
    unique_values = {}

    for sample in samples:
      for k, v in sample['metadata'].items():
        if len(unique_values.setdefault(k, set())) < 2 and v.__hash__:
          unique_values[k].add(v)

    # Find keys which are not present in all samples
    for sample in samples:
      for k in frozenset(unique_values) - frozenset(sample['metadata']):
        unique_values[k].add(None)

    return frozenset(
        k for k, v in unique_values.items() if len(v) == 1 and None not in v
    )

  def _FormatMetadata(self, metadata):
    """Format 'metadata' as space-delimited key="value" pairs."""
    return ' '.join('{}="{}"'.format(k, v) for k, v in sorted(metadata.items()))

  def PublishSamples(self, samples):
    # result will store the formatted text, then be emitted to self.stream and
    # logged.
    result = six.StringIO()
    dashes = '-' * 25
    result.write(
        '\n' + dashes + 'PerfKitBenchmarker Results Summary' + dashes + '\n'
    )

    if not samples:
      logging.debug(
          'Pretty-printing results to %s:\n%s', self.stream, result.getvalue()
      )
      self.stream.write(result.getvalue())
      return

    key = operator.itemgetter('test')
    samples = sorted(samples, key=key)
    globally_constant_keys = self._FindConstantMetadataKeys(samples)

    for benchmark, test_samples in itertools.groupby(samples, key):
      test_samples = list(test_samples)
      # Drop end-to-end runtime: it always has no metadata.
      non_endtoend_samples = [
          i for i in test_samples if i['metric'] != 'End to End Runtime'
      ]
      locally_constant_keys = (
          self._FindConstantMetadataKeys(non_endtoend_samples)
          - globally_constant_keys
      )
      all_constant_meta = globally_constant_keys.union(locally_constant_keys)

      benchmark_meta = {
          k: v
          for k, v in test_samples[0]['metadata'].items()
          if k in locally_constant_keys
      }
      result.write('{}:\n'.format(benchmark.upper()))

      if benchmark_meta:
        result.write('  {}\n'.format(self._FormatMetadata(benchmark_meta)))

      for sample in test_samples:
        meta = {
            k: v
            for k, v in sample['metadata'].items()
            if k not in all_constant_meta
        }
        result.write(
            '  {:<30s} {:>15f} {:<30s}'.format(
                sample['metric'], sample['value'], sample['unit']
            )
        )
        if meta:
          result.write(' ({})'.format(self._FormatMetadata(meta)))
        result.write('\n')

    global_meta = {
        k: v
        for k, v in samples[0]['metadata'].items()
        if k in globally_constant_keys
    }
    result.write('\n' + dashes + '\n')
    result.write(
        'For all tests: {}\n'.format(self._FormatMetadata(global_meta))
    )

    value = result.getvalue()
    logging.debug('Pretty-printing results to %s:\n%s', self.stream, value)
    self.stream.write(value)


class LogPublisher(SamplePublisher):
  """Writes samples to a Python Logger.

  Attributes:
    level: Logging level. Defaults to logging.INFO.
    logger: Logger to publish to. Defaults to the root logger.
  """

  PUBLISH_CONSOLE_LOG_DATA = False

  def __init__(self, level=logging.INFO, logger=None):
    super().__init__()
    self.level = level
    self.logger = logger or logging.getLogger()
    self._pprinter = pprint.PrettyPrinter()

  def __repr__(self):
    return '<{} logger={} level={}>'.format(
        type(self).__name__, self.logger, self.level
    )

  def PublishSamples(self, samples):
    header = '\n' + '-' * 25 + 'PerfKitBenchmarker Complete Results' + '-' * 25
    self.logger.log(self.level, header)
    for sample in samples:
      self.logger.log(self.level, self._pprinter.pformat(sample))


# TODO: Extract a function to write delimited JSON to a stream.
class NewlineDelimitedJSONPublisher(SamplePublisher):
  """Publishes samples to a file as newline delimited JSON.

  The resulting output file is compatible with 'bq load' using
  format NEWLINE_DELIMITED_JSON.

  If 'collapse_labels' is True, metadata is converted to a flat string with key
  'labels' via GetLabelsFromDict.

  Attributes:
    file_path: string. Destination path to write samples.
    mode: Open mode for 'file_path'. Set to 'a' to append.
    collapse_labels: boolean. If true, collapse sample metadata.
  """

  def __init__(self, file_path, mode='wt', collapse_labels=True):
    super().__init__()
    self.file_path = file_path
    self.mode = mode
    self.collapse_labels = collapse_labels

  def __repr__(self):
    return '<{} file_path="{}" mode="{}">'.format(
        type(self).__name__, self.file_path, self.mode
    )

  def PublishSamples(self, samples):
    logging.info('Publishing %d samples to %s', len(samples), self.file_path)
    with open(self.file_path, self.mode) as fp:
      fcntl.flock(fp, fcntl.LOCK_EX)
      for sample in samples:
        sample = sample.copy()
        if self.collapse_labels:
          sample['labels'] = GetLabelsFromDict(sample.pop('metadata', {}))
        fp.write(json.dumps(sample) + '\n')


class BigQueryPublisher(SamplePublisher):
  """Publishes samples to BigQuery.

  Attributes:
    bigquery_table: string. The bigquery table to publish to, of the form
      '[project_name:]dataset_name.table_name'
    project_id: string. Project to use for authenticating with BigQuery.
    bq_path: string. Path to the 'bq' executable'.
    service_account: string. Use this service account email address for
      authorization. For example, 1234567890@developer.gserviceaccount.com
    service_account_private_key_file: Filename that contains the service account
      private key. Must be specified if service_account is specified.
    application_default_credential_file: Filename that holds Google applciation
      default credentials. Cannot be set alongside service_account.
  """

  def __init__(
      self,
      bigquery_table,
      project_id=None,
      bq_path='bq',
      service_account=None,
      service_account_private_key_file=None,
      application_default_credential_file=None,
  ):
    super().__init__()
    self.bigquery_table = bigquery_table
    self.project_id = project_id
    self.bq_path = bq_path
    self.service_account = service_account
    self.service_account_private_key_file = service_account_private_key_file
    self._credentials_file = vm_util.PrependTempDir(DEFAULT_CREDENTIALS_JSON)
    self.application_default_credential_file = (
        application_default_credential_file
    )

    if (self.service_account is None) != (
        self.service_account_private_key_file is None
    ):
      raise ValueError(
          'service_account and service_account_private_key '
          'must be specified together.'
      )
    if (
        application_default_credential_file is not None
        and self.service_account is not None
    ):
      raise ValueError(
          'application_default_credential_file cannot be used '
          'alongside service_account.'
      )

  def __repr__(self):
    return '<{} table="{}">'.format(type(self).__name__, self.bigquery_table)

  def PublishSamples(self, samples):
    if not samples:
      logging.warning('No samples: not publishing to BigQuery')
      return

    with vm_util.NamedTemporaryFile(
        prefix='perfkit-bq-pub', dir=vm_util.GetTempDir(), suffix='.json'
    ) as tf:
      json_publisher = NewlineDelimitedJSONPublisher(
          tf.name, collapse_labels=True
      )
      json_publisher.PublishSamples(samples)
      tf.close()
      logging.info(
          'Publishing %d samples to %s', len(samples), self.bigquery_table
      )
      load_cmd = [self.bq_path]
      if self.project_id:
        load_cmd.append('--project_id=' + self.project_id)
      if self.service_account:
        assert self.service_account_private_key_file is not None
        load_cmd.extend([
            '--service_account=' + self.service_account,
            '--service_account_credential_file=' + self._credentials_file,
            '--service_account_private_key_file='
            + self.service_account_private_key_file,
        ])
      elif self.application_default_credential_file is not None:
        load_cmd.append(
            '--application_default_credential_file='
            + self.application_default_credential_file
        )
      load_cmd.extend([
          'load',
          '--autodetect',
          '--source_format=NEWLINE_DELIMITED_JSON',
          self.bigquery_table,
          tf.name,
      ])
      vm_util.IssueRetryableCommand(load_cmd)


class CloudStoragePublisher(SamplePublisher):
  """Publishes samples to a Google Cloud Storage bucket using gsutil.

  Samples are formatted using a NewlineDelimitedJSONPublisher, and written to a
  the destination file within the specified bucket named:

    <time>_<uri>

  where <time> is the number of milliseconds since the Epoch, and <uri> is a
  random UUID.
  """

  def __init__(self, bucket, gsutil_path='gsutil', sub_folder=None):
    """CloudStoragePublisher constructor.

    Args:
      bucket: string. The GCS bucket name to publish to.
      gsutil_path: string. The path to the 'gsutil' tool.
      sub_folder: Optional folder within the bucket to publish to.
    """
    super().__init__()
    self.gsutil_path = gsutil_path
    if sub_folder:
      self.gcs_directory = f'gs://{bucket}/{sub_folder}'
    else:
      self.gcs_directory = f'gs://{bucket}'

  def __repr__(self):
    return f'<{type(self).__name__} gcs_directory="{self.gcs_directory}">'

  def _GenerateObjectName(self):
    object_name = str(int(time.time() * 100)) + '_' + str(uuid.uuid4())
    return object_name[:GCS_OBJECT_NAME_LENGTH]

  def PublishSamples(self, samples):
    with vm_util.NamedTemporaryFile(
        prefix='perfkit-gcs-pub', dir=vm_util.GetTempDir(), suffix='.json'
    ) as tf:
      json_publisher = NewlineDelimitedJSONPublisher(tf.name)
      json_publisher.PublishSamples(samples)
      tf.close()
      object_name = self._GenerateObjectName()
      storage_uri = f'{self.gcs_directory}/{object_name}'
      logging.info('Publishing %d samples to %s', len(samples), storage_uri)
      copy_cmd = [self.gsutil_path, 'cp', tf.name, storage_uri]
      vm_util.IssueRetryableCommand(copy_cmd)


class ElasticsearchPublisher(SamplePublisher):
  """Publish samples to an Elasticsearch server.

  Index and document type will be created if they do not exist.
  """

  def __init__(self, es_uri=None, es_index=None, es_type=None):
    """ElasticsearchPublisher constructor.

    Args:
      es_uri: String. e.g. "http://localhost:9200"
      es_index: String. Default "perfkit"
      es_type: String. Default "result"
    """
    super().__init__()
    self.es_uri = es_uri
    self.es_index = es_index.lower()
    self.es_type = es_type
    self.mapping_5_plus = {
        'mappings': {
            'result': {
                'numeric_detection': True,
                'properties': {
                    'timestamp': {
                        'type': 'date',
                        'format': 'yyyy-MM-dd HH:mm:ss.SSSSSS',
                    },
                    'value': {'type': 'double'},
                },
                'dynamic_templates': [{
                    'strings': {
                        'match_mapping_type': 'string',
                        'mapping': {
                            'type': 'text',
                            'fields': {
                                'raw': {
                                    'type': 'keyword',
                                    'ignore_above': 256,
                                }
                            },
                        },
                    }
                }],
            }
        }
    }

    self.mapping_before_5 = {
        'mappings': {
            'result': {
                'numeric_detection': True,
                'properties': {
                    'timestamp': {
                        'type': 'date',
                        'format': 'yyyy-MM-dd HH:mm:ss.SSSSSS',
                    },
                    'value': {'type': 'double'},
                },
                'dynamic_templates': [{
                    'strings': {
                        'match_mapping_type': 'string',
                        'mapping': {
                            'type': 'string',
                            'fields': {
                                'raw': {
                                    'type': 'string',
                                    'index': 'not_analyzed',
                                }
                            },
                        },
                    }
                }],
            }
        }
    }

  def PublishSamples(self, samples):
    """Publish samples to Elasticsearch service."""
    try:
      # pylint:disable=g-import-not-at-top
      from elasticsearch import Elasticsearch  # pytype: disable=import-error
      # pylint:enable=g-import-not-at-top
    except ImportError:
      raise ImportError(
          'The "elasticsearch" package is required to use '
          'the Elasticsearch publisher. Please make sure it '
          'is installed.'
      )

    es = Elasticsearch([self.es_uri])
    if not es.indices.exists(index=self.es_index):
      # choose whether to use old or new mapings based on
      # the version of elasticsearch that is being used
      if int(es.info()['version']['number'].split('.')[0]) >= 5:
        es.indices.create(index=self.es_index, body=self.mapping_5_plus)
        logging.info(
            'Create index %s and default mappings for'
            ' elasticsearch version >= 5.0.0',
            self.es_index,
        )
      else:
        es.indices.create(index=self.es_index, body=self.mapping_before_5)
        logging.info(
            'Create index %s and default mappings for'
            ' elasticsearch version < 5.0.0',
            self.es_index,
        )
    for s in samples:
      sample = copy.deepcopy(s)
      # Make timestamp understandable by ES and human.
      sample['timestamp'] = self._FormatTimestampForElasticsearch(
          sample['timestamp']
      )
      # Keys cannot have dots for ES
      sample = self._deDotKeys(sample)
      # Add sample to the "perfkit index" of "result type" and using sample_uri
      # as each ES's document's unique _id
      es.create(
          index=self.es_index,
          doc_type=self.es_type,
          id=sample['sample_uri'],
          body=json.dumps(sample),
      )

  # pylint: disable=g-doc-args,g-doc-return-or-yield,g-short-docstring-punctuation
  def _FormatTimestampForElasticsearch(self, epoch_us):
    """Convert the floating epoch timestamp in micro seconds epoch_us to

    yyyy-MM-dd HH:mm:ss.SSSSSS in string
    """
    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(epoch_us))
    num_dec = ('%.6f' % (epoch_us - math.floor(epoch_us))).split('.')[1]
    new_ts = '%s.%s' % (ts, num_dec)
    return new_ts

  # pylint: enable=g-doc-args,g-doc-return-or-yield,g-short-docstring-punctuation

  # pylint: disable-next=invalid-name
  def _deDotKeys(self, res):
    """Recursively replace dot with underscore in all keys in a dictionary."""
    for key, value in res.items():
      if isinstance(value, dict):
        self._deDotKeys(value)
      new_key = key.replace('.', '_')
      if new_key != key:
        res[new_key] = res.pop(key)
    return res


class InfluxDBPublisher(SamplePublisher):
  """Publisher writes samples to InfluxDB.

  Attributes:
    influx_uri: Takes in type string. Consists of the Influx DB address and
      port.Expects the format hostname:port
    influx_db_name: Takes in tupe string. Consists of the name of Influx DB
      database that you wish to publish to or create.
  """

  def __init__(self, influx_uri=None, influx_db_name=None):
    super().__init__()
    # set to default above in flags unless changed
    self.influx_uri = influx_uri
    self.influx_db_name = influx_db_name

  def PublishSamples(self, samples):
    formated_samples = []
    for sample in samples:
      formated_samples.append(self._ConstructSample(sample))
    self._Publish(formated_samples)

  def _Publish(self, formated_samples):
    try:
      self._CreateDB()
      body = '\n'.join(formated_samples)
      self._WriteData(body)
    except (OSError, httplib.HTTPException) as http_exception:
      logging.error('Error connecting to the database:  %s', http_exception)

  # pylint: disable=missing-function-docstring
  def _ConstructSample(self, sample):
    sample['product_name'] = FLAGS.product_name
    timestamp = str(int((10**9) * sample['timestamp']))
    measurement = 'perfkitbenchmarker'

    tag_set_metadata = ''
    if 'metadata' in sample:
      if sample['metadata']:
        tag_set_metadata = ','.join(self._FormatToKeyValue(sample['metadata']))
    tag_keys = (
        'test',
        'official',
        'owner',
        'run_uri',
        'sample_uri',
        'metric',
        'unit',
        'product_name',
    )
    ordered_tags = collections.OrderedDict([(k, sample[k]) for k in tag_keys])
    tag_set = ','.join(self._FormatToKeyValue(ordered_tags))
    if tag_set_metadata:
      tag_set += ',' + tag_set_metadata

    field_set = '%s=%s' % ('value', sample['value'])

    sample_constructed_body = '%s,%s %s %s' % (
        measurement,
        tag_set,
        field_set,
        timestamp,
    )
    return sample_constructed_body

  def _FormatToKeyValue(self, sample):
    key_value_pairs = []
    for k, v in sample.items():
      if v == '':
        v = '\\"\\"'
      v = str(v)
      v = v.replace(',', r'\,')
      v = v.replace(' ', r'\ ')
      key_value_pairs.append('%s=%s' % (k, v))
    return key_value_pairs

  def _CreateDB(self):
    """Creates a database.

    This method is idempotent. If the DB already exists it will simply
    return a 200 code without re-creating it.
    """
    successful_http_request_codes = [200, 202, 204]
    header = {
        'Content-type': 'application/x-www-form-urlencoded',
        'Accept': 'text/plain',
    }
    params = urllib.parse.urlencode(
        {'q': 'CREATE DATABASE ' + self.influx_db_name}
    )
    conn = httplib.HTTPConnection(self.influx_uri)
    conn.request('POST', '/query?' + params, headers=header)
    response = conn.getresponse()
    conn.close()
    if response.status in successful_http_request_codes:
      logging.debug('Success! %s DB Created', self.influx_db_name)
    else:
      logging.error(
          '%d Request could not be completed due to: %s',
          response.status,
          response.reason,
      )
      raise httplib.HTTPException

  # pylint: disable=missing-function-docstring
  def _WriteData(self, data):
    successful_http_request_codes = [200, 202, 204]
    params = data
    header = {'Content-type': 'application/octet-stream'}
    conn = httplib.HTTPConnection(self.influx_uri)
    conn.request(
        'POST', '/write?' + 'db=' + self.influx_db_name, params, headers=header
    )
    response = conn.getresponse()
    conn.close()
    if response.status in successful_http_request_codes:
      logging.debug('Writing samples to publisher: writing samples.')
    else:
      logging.error(
          '%d Request could not be completed due to: %s %s',
          response.status,
          response.reason,
          data,
      )
      raise httplib.HTTPException


class SampleCollector:
  """A performance sample collector.

  Supports incorporating additional metadata into samples, and publishing
  results via any number of SamplePublishers.

  Attributes:
    samples: A list of Sample objects as dicts that have yet to be published.
    published_samples: A list of Sample objects as dicts that have been
      published.
    metadata_providers: A list of MetadataProvider objects. Metadata providers
      to use.  Defaults to DEFAULT_METADATA_PROVIDERS.
    publishers: A list of SamplePublisher objects to publish to.
    publishers_from_flags: If True, construct publishers based on FLAGS and add
      those to the publishers list.
    add_default_publishers: If True, add a LogPublisher,
      PrettyPrintStreamPublisher, and NewlineDelimitedJSONPublisher targeting
      the run directory to the publishers list.
    run_uri: A unique tag for the run.
  """

  def __init__(
      self,
      metadata_providers=None,
      publishers=None,
      publishers_from_flags=True,
      add_default_publishers=True,
  ):
    # List of samples yet to be published.
    self.samples: list[pkb_sample.SampleDict] = []
    # List of samples that have already been published.
    self.published_samples: list[pkb_sample.SampleDict] = []

    if metadata_providers is not None:
      self.metadata_providers = metadata_providers
    else:
      self.metadata_providers = DEFAULT_METADATA_PROVIDERS

    self.publishers: list[SamplePublisher] = publishers[:] if publishers else []
    for publisher_class in EXTERNAL_PUBLISHERS:
      self.publishers.append(publisher_class())
    if publishers_from_flags:
      self.publishers.extend(SampleCollector._PublishersFromFlags())
    if add_default_publishers:
      self.publishers.extend(SampleCollector._DefaultPublishers())

    logging.debug('Using publishers: %s', str(self.publishers))

  @classmethod
  def _DefaultPublishers(cls):
    """Gets a list of default publishers."""
    publishers = []
    if FLAGS.record_log_publisher:
      publishers.append(LogPublisher())
    publishers.append(PrettyPrintStreamPublisher())

    return publishers

  @classmethod
  def _PublishersFromFlags(cls):
    publishers = []

    if JSON_PATH.value:
      # Default publishing path needs to be qualified with the run_uri temp dir.
      if JSON_PATH.value == DEFAULT_JSON_OUTPUT_NAME:
        publishing_json_path = vm_util.PrependTempDir(JSON_PATH.value)
      else:
        publishing_json_path = JSON_PATH.value
      publishers.append(
          NewlineDelimitedJSONPublisher(
              publishing_json_path,
              mode=FLAGS.json_write_mode,
              collapse_labels=FLAGS.collapse_labels,
          )
      )

    if FLAGS.bigquery_table:
      publishers.append(
          BigQueryPublisher(
              FLAGS.bigquery_table,
              project_id=FLAGS.bq_project,
              bq_path=FLAGS.bq_path,
              service_account=FLAGS.service_account,
              service_account_private_key_file=FLAGS.service_account_private_key,
              application_default_credential_file=FLAGS.application_default_credential_file,
          )
      )

    if FLAGS.cloud_storage_bucket:
      publishers.append(
          CloudStoragePublisher(
              FLAGS.cloud_storage_bucket, gsutil_path=FLAGS.gsutil_path
          )
      )
    if PARTITIONED_GCS_URL.value:
      now = datetime.datetime.now(tz=_PACIFIC_TZ)
      publishers.append(
          CloudStoragePublisher(
              PARTITIONED_GCS_URL.value,
              sub_folder=now.strftime('%Y/%m/%d/%H'),
              gsutil_path=FLAGS.gsutil_path,
          )
      )
    if FLAGS.csv_path:
      publishers.append(CSVPublisher(FLAGS.csv_path))

    if FLAGS.es_uri:
      publishers.append(
          ElasticsearchPublisher(
              es_uri=FLAGS.es_uri,
              es_index=FLAGS.es_index,
              es_type=FLAGS.es_type,
          )
      )
    if FLAGS.influx_uri:
      publishers.append(
          InfluxDBPublisher(
              influx_uri=FLAGS.influx_uri, influx_db_name=FLAGS.influx_db_name
          )
      )

    return publishers

  def AddSamples(self, samples, benchmark, benchmark_spec):
    """Adds data samples to the publisher.

    Args:
      samples: A list of Sample objects.
      benchmark: string. The name of the benchmark.
      benchmark_spec: BenchmarkSpec. Benchmark specification.
    """
    for s in samples:
      # Annotate the sample.
      sample: pkb_sample.SampleDict = s.asdict()
      sample['test'] = benchmark

      for meta_provider in self.metadata_providers:
        sample['metadata'] = meta_provider.AddMetadata(
            sample['metadata'], benchmark_spec
        )

      sample['product_name'] = FLAGS.product_name
      sample['official'] = FLAGS.official
      sample['owner'] = FLAGS.owner
      sample['run_uri'] = benchmark_spec.uuid
      sample['sample_uri'] = str(uuid.uuid4())
      self.samples.append(sample)

  def PublishSamples(self):
    """Publish samples via all registered publishers."""
    if not self.samples:
      logging.warning('No samples to publish.')
      return
    samples_for_console = []
    for s in self.samples:
      if not s.get(pkb_sample.DISABLE_CONSOLE_LOG, False):
        samples_for_console.append(s)
    for publisher in self.publishers:
      publisher.PublishSamples(
          self.samples
          if publisher.PUBLISH_CONSOLE_LOG_DATA
          else samples_for_console
      )
    self.published_samples += self.samples
    self.samples = []


def RepublishJSONSamples(path):
  """Read samples from a JSON file and re-export them.

  Args:
    path: the path to the JSON file.
  """

  with open(path) as file:
    samples = [json.loads(s) for s in file if s]
  for sample in samples:
    # Chop '|' at the beginning and end of labels and split labels by '|,|'
    fields = sample.pop('labels')[1:-1].split('|,|')
    # Turn the fields into [[key, value], ...]
    key_values = [field.split(':', 1) for field in fields]
    sample['metadata'] = {k: v for k, v in key_values}

  # We can't use a SampleCollector because SampleCollector.AddSamples depends on
  # having a benchmark and a benchmark_spec.
  publishers = SampleCollector._PublishersFromFlags()
  for publisher in publishers:
    publisher.PublishSamples(samples)


if __name__ == '__main__':
  log_util.ConfigureBasicLogging()

  try:
    argv = FLAGS(sys.argv)
  except flags.Error as e:
    logging.error(e)
    logging.info('Flag error. Usage: publisher.py <flags> path-to-json-file')
    sys.exit(1)

  if len(argv) != 2:
    logging.info(
        'Argument number error. Usage: publisher.py <flags> path-to-json-file'
    )
    sys.exit(1)

  json_path = argv[1]

  RepublishJSONSamples(json_path)
