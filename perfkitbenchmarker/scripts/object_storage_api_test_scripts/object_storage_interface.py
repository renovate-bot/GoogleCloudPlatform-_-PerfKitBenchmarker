# Copyright 2016 PerfKitBenchmarker Authors. All rights reserved.
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

"""The generic superclass for object storage API providers."""

import abc


class ObjectStorageServiceBase(metaclass=abc.ABCMeta):
  """Our interface to an object storage service."""

  @abc.abstractmethod
  def __init__(self):
    """Create the service object."""

    # __init__ takes no arguments because there are no universal
    # arguments that apply to all services. Instead, each service
    # should look at FLAGS, get the configuration it needs, and raise
    # an exception if the flags are invalid for its particular
    # service.

    pass

  @abc.abstractmethod
  def ListObjects(self, bucket, prefix):
    """List the objects in a bucket given a prefix.

    Args:
      bucket: the name of the bucket.
      prefix: a prefix to list from.

    Returns:
      A list of object names.
    """

    pass

  @abc.abstractmethod
  def DeleteObjects(
      self,
      bucket,
      objects_to_delete,
      objects_deleted=None,
      delay_time=0,
      object_sizes=None,
  ):
    """Delete a list of objects.

    Args:
      bucket: the name of the bucket.
      objects_to_delete: a list of names of objects to delete.
      objects_deleted: if given, a list to record the objects that have been
        successfully deleted.
      delay_time: time to delay each API call by
      object_sizes: if given, a list of sizes of the objects to delete

    Returns:
      A tuple of (start_times, latencies, sizes).
    """

    pass

  @abc.abstractmethod
  def BulkDeleteObjects(self, bucket, objects_to_delete, delay_time):
    """Delete a list of objects using the bulk API request.

    Args:
      bucket: the name of the bucket.
      objects_to_delete: a list of names of objects to delete.
      delay_time: time to delay each API call by

    Returns:
      A tuple of (start_time, latency).
    """

    pass

  @abc.abstractmethod
  def WriteObjectFromBuffer(self, bucket, object_name, stream, size):
    """Write an object to a bucket.

    Exceptions are propagated to the caller, which can decide whether
    to tolerate them or not. This function will seek() to the
    beginning of stream before sending.

    Args:
      bucket: the name of the bucket to write to.
      object_name: the name of the object.
      stream: a read()-able and seek()-able stream to transfer.
      size: the number of bytes to transfer.

    Returns:
      a tuple of (start_time, latency).
    """

    pass

  @abc.abstractmethod
  def ReadObject(self, bucket, object_name):
    """Read an object.

    Exceptions are propagated to the caller, which can decide whether
    to tolerate them or not.

    Args:
      bucket: the name of the bucket.
      object_name: the name of the object.

    Returns:
      A tuple of (start_time, latency)
    """

    pass
