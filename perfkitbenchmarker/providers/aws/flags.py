# Copyright 2015 PerfKitBenchmarker Authors. All rights reserved.
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
"""Module containing flags applicable across benchmark run on AWS."""

from typing import Any

from absl import flags

flags.DEFINE_string(
    'aws_user_name',
    '',
    'This determines the user name that Perfkit will '
    'attempt to use. Defaults are OS specific.',
)
flags.DEFINE_integer(
    'aws_provisioned_iops',
    None,
    'IOPS for Provisioned IOPS (SSD) volumes in AWS.',
)
flags.DEFINE_integer(
    'aws_provisioned_throughput',
    None,
    'Provisioned throughput (MB/s) for (SSD) volumes in AWS.',
)
AWS_NIC_QUEUE_COUNTS = flags.DEFINE_list(
    'aws_nic_queue_counts',
    None,
    'The queue count of each NIC. Specify a list of key=value pairs, where key'
    ' is the network device name and value is the queue count.',
)

flags.DEFINE_string(
    'aws_dax_node_type',
    'dax.r4.large',
    'The node type used for creating AWS DAX cluster.',
)
flags.DEFINE_integer(
    'aws_dax_replication_factor',
    3,
    'The replication factor of AWS DAX cluster.',
)
flags.DEFINE_string(
    'aws_emr_loguri',
    None,
    'The log-uri parameter to pass to AWS when creating a '
    'cluster.  If not set, a bucket will be created.',
)
flags.DEFINE_integer(
    'aws_emr_job_wait_time',
    18000,
    'The time to wait for an EMR job to finish, in seconds',
)
flags.DEFINE_boolean(
    'aws_spot_instances',
    False,
    'Whether to use AWS spot instances for any AWS VMs.',
)
flags.DEFINE_float(
    'aws_spot_price',
    None,
    'The spot price to bid for AWS spot instances. Defaults '
    'to on-demand price when left as None.',
)
flags.DEFINE_enum(
    'aws_spot_block_duration_minutes',
    None,
    ['60', '120', '180', '240', '300', '360'],
    'The required '
    'duration for the Spot Instances (also known as Spot blocks),'
    ' in minutes. This value must be a multiple of 60.',
)
flags.DEFINE_integer(
    'aws_boot_disk_size', None, 'The boot disk size in GiB for AWS VMs.'
)
flags.DEFINE_string('kops', 'kops', 'The path to the kops binary.')
flags.DEFINE_string(
    'aws_image_name_filter',
    None,
    'The filter to use when searching for an image for a VM. '
    'See usage details in aws_virtual_machine.py around '
    'IMAGE_NAME_FILTER.',
)
flags.DEFINE_string(
    'aws_image_name_regex',
    None,
    'The Python regex to use to further filter images for a '
    'VM. This applies after the aws_image_name_filter. See '
    'usage details in aws_virtual_machine.py around '
    'IMAGE_NAME_REGEX.',
)
AWS_PREPROVISIONED_DATA_BUCKET = flags.DEFINE_string(
    'aws_preprovisioned_data_bucket',
    None,
    'AWS bucket where pre-provisioned data has been copied. '
    'Requires --aws_ec2_instance_profile to be set.',
)
ELASTICACHE_NODE_TYPE = flags.DEFINE_string(
    'elasticache_node_type',
    'cache.m4.large',
    'The AWS cache node type to use for elasticache clusters.',
)
ELASTICACHE_FAILOVER_ZONE = flags.DEFINE_string(
    'elasticache_failover_zone', None, 'AWS elasticache failover zone'
)
flags.DEFINE_string(
    'aws_efs_token',
    None,
    'The creation token used to create the EFS resource. '
    'If the file system already exists, it will use that '
    'instead of creating a new one.',
)
flags.DEFINE_boolean(
    'aws_delete_file_system', True, 'Whether to delete the EFS file system.'
)
flags.DEFINE_enum(
    'efs_throughput_mode',
    'provisioned',
    ['provisioned', 'bursting'],
    'The throughput mode to use for EFS.',
)
flags.DEFINE_float(
    'efs_provisioned_throughput',
    1024.0,
    'The throughput limit of EFS (in MiB/s) when run in provisioned mode.',
)
flags.DEFINE_boolean(
    'provision_athena', False, 'Whether to provision the Athena database.'
)
flags.DEFINE_boolean(
    'teardown_athena', True, 'Whether to teardown the Athena database.'
)
flags.DEFINE_string(
    'athena_output_location_prefix',
    'athena-cli-results',
    'Prefix of the S3 bucket name for Athena Query Output. Suffix will be the '
    'region and the run URI, and the bucket will be dynamically created and '
    'deleted during the test.',
)
flags.DEFINE_string('eksctl', 'eksctl', 'Path to eksctl.')
flags.DEFINE_enum(
    'redshift_client_interface',
    'JDBC',
    ['JDBC'],
    'The Runtime Interface used when interacting with Redshift.',
)
flags.DEFINE_enum(
    'athena_client_interface',
    'JAVA',
    ['JAVA'],
    'The Runtime Interface used when interacting with Athena.',
)
flags.DEFINE_string('athena_query_timeout', '600', 'Query timeout in seconds.')
flags.DEFINE_string(
    'athena_workgroup',
    '',
    'Use athena workgroup to separate applications and choose '
    'execution configuration like the engine version.',
)
flags.DEFINE_boolean(
    'athena_metrics_collection',
    False,
    'Should the cloud watch metrics be collected for Athena query executions.',
)
flags.DEFINE_boolean(
    'athena_workgroup_delete',
    True,
    'Should the dedicated athena workgroups be deleted or kept alive for'
    ' investigations.',
)
flags.DEFINE_enum(
    'aws_credit_specification',
    None,
    ['CpuCredits=unlimited', 'CpuCredits=standard'],
    'Credit specification for burstable vms.',
)
flags.DEFINE_boolean(
    'aws_vm_hibernate',
    False,
    'Whether to hibernate(suspend) an aws vminstance.',
)
flags.DEFINE_string(
    'aws_glue_crawler_role',
    None,
    "Role's ARN to be used by the crawler. Must have policies that grant "
    'permission for using AWS Glue and read access to S3.',
)
flags.DEFINE_string(
    'aws_glue_job_role',
    None,
    "Role's ARN to be used by Glue Jobs. Must have policies that grant "
    'permission for using AWS Glue and read access to S3.',
)
flags.DEFINE_string(
    'aws_emr_serverless_role',
    None,
    "Role's ARN to be used by AWS EMR Serverless Jobs. Must have policies that "
    'grant AWS Serverless read & write S3 access.',
)
flags.DEFINE_integer(
    'aws_glue_crawler_sample_size',
    None,
    'Sets how many files will be crawled in each leaf directory. If left '
    'unset, all the files will be crawled. May range from 1 to 249.',
    1,
    249,
)
AWS_CAPACITY_BLOCK_RESERVATION_ID = flags.DEFINE_string(
    'aws_capacity_block_reservation_id',
    None, 'Reservation id for capacity block.')
AWS_CREATE_DISKS_WITH_VM = flags.DEFINE_boolean(
    'aws_create_disks_with_vm',
    True,
    'Whether to create disks at VM creation time. Defaults to True.',
)
# TODO(user): Create a spec for aurora.
AURORA_STORAGE_TYPE = flags.DEFINE_enum(
    'aws_aurora_storage_type',
    'aurora',
    ['aurora', 'aurora-iopt1'],
    'Aurora storage type to use, corresponds to different modes of billing. See'
    ' https://aws.amazon.com/rds/aurora/pricing/.',
)
AWS_EC2_INSTANCE_PROFILE = flags.DEFINE_string(
    'aws_ec2_instance_profile',
    None,
    'The instance profile to use for EC2 instances. '
    'Allows calling APIs on the instance.',
)
AWS_EKS_POD_IDENTITY_ROLE = flags.DEFINE_string(
    'aws_eks_pod_identity_role',
    None,
    'The IAM role to associate with the pod identity for EKS clusters.',
)

PCLUSTER_PATH = flags.DEFINE_string(
    'pcluster_path',
    'pcluster',
    'The path for the pcluster (parallel-cluster) utility.',
)


@flags.multi_flags_validator(
    [
        AWS_PREPROVISIONED_DATA_BUCKET.name,
        AWS_EC2_INSTANCE_PROFILE.name,
        AWS_EKS_POD_IDENTITY_ROLE.name,
    ],
    message=(
        '--aws_ec2_instance_profile or --aws_eks_pod_identity_role must be '
        'set if --aws_preprovisioned_data_bucket is set.'
    ),
)
def _ValidatePreprovisionedDataAccess(flag_values: dict[str, Any]) -> bool:
  """Validates that an auth flag is set if preprovisioned data is used."""
  return bool(
      not flag_values[AWS_PREPROVISIONED_DATA_BUCKET.name]
      or flag_values[AWS_EC2_INSTANCE_PROFILE.name]
      or flag_values[AWS_EKS_POD_IDENTITY_ROLE.name]
  )

# MemoryDB Flags
MEMORYDB_NODE_TYPE = flags.DEFINE_string(
    'aws_memorydb_node_type',
    'db.r7g.large',
    'The AWS node type to use for MemoryDB clusters.',
)
MEMORYDB_FAILOVER_ZONE = flags.DEFINE_string(
    'aws_memorydb_failover_zone', None, 'AWS MemoryDB failover zone'
)
