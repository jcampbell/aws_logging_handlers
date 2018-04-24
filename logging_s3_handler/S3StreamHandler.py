__author__ = 'Omri Eival'

from logging import StreamHandler
from io import BufferedIOBase, BytesIO
from boto3 import Session
from datetime import datetime

import atexit
import signal
import threading
import queue

DEFAULT_CHUNK_SIZE = 5 * 1024 ** 2  # 5 MB
DEFAULT_ROTATION_TIME_SECS = 12 * 60 * 60  # 12 hours
MAX_FILE_SIZE_BYTES = 100 * 1024 ** 2  # 100 MB
MIN_WORKERS_NUM = 2


class Task:
    def __init__(self, callable_func, *args, **kwargs):
        assert callable(callable_func), "First argument in task should be callable"
        self.callable_func = callable_func
        self.args = args
        self.kwargs = kwargs


def task_worker(q):
    while True:
        if not q.empty():
            task = q.get()
            if task == -1:
                break
            assert isinstance(task, (Task,)), "task should be of type `Task` only!"
            task.callable_func(*task.args, **task.kwargs)
            q.task_done()


class StreamObject:
    """
    Class representation of the s3 object along with all the needed metadata to stream to S3
    """

    def __init__(self, s3_resource, bucket_name, filename, buffer_queue):
        self.object = s3_resource.Object(bucket_name, filename)
        self.uploader = self.object.initiate_multipart_upload()
        self.bucket = bucket_name
        try:
            total_bytes = s3_resource.meta.client.head_object(Bucket=self.bucket.name, Key=filename)
        except Exception:
            total_bytes = 0

        self.buffer = BytesIO()
        self.chunk_count = 0
        self.byte_count = total_bytes
        self.parts = []
        self.tasks = buffer_queue

    def add_task(self, task):
        self.tasks.put(task)

    def join_tasks(self):
        self.tasks.join()


class S3Streamer(BufferedIOBase):
    """
    The stream interface used by the handler which binds to S3 and utilizes the object class
    """

    _stream_buffer_queue = queue.Queue()
    _rotation_queue = queue.Queue()

    def __init__(self, bucket, key_id, secret, key, chunk_size=DEFAULT_CHUNK_SIZE,
                 max_file_log_time=DEFAULT_ROTATION_TIME_SECS, max_file_size_bytes=MAX_FILE_SIZE_BYTES,
                 encoder='utf-8', workers=2):

        self.session = Session(key_id, secret)
        self.s3 = self.session.resource('s3')
        self.start_time = int(datetime.utcnow().strftime('%s'))
        self.key = key
        self.chunk_size = chunk_size
        self.max_file_log_time = max_file_log_time
        self.max_file_size_bytes = max_file_size_bytes
        self.current_file_name = "{}_{}".format(key, int(datetime.utcnow().strftime('%s')))
        self.encoder = encoder

        try:
            self.s3.meta.client.head_bucket(Bucket=bucket)
        except Exception:
            raise ValueError('Bucket %s does not exist, or missing permissions' % bucket)

        self._bucket = self.s3.Bucket(bucket)
        self._current_object = self._get_stream_object(self.current_file_name)
        self.workers = [threading.Thread(target=task_worker, args=(self._rotation_queue,)).start() for _ in
                        range(int(max(workers, MIN_WORKERS_NUM) / 2) + 1)]
        self.stream_bg_workers = [threading.Thread(target=task_worker, args=(self._stream_buffer_queue,)).start() for _
                                  in range(max(int(max(workers, MIN_WORKERS_NUM) / 2), 1))]

        self._is_open = True

        BufferedIOBase.__init__(self)

    def add_task(self, task):
        self._rotation_queue.put(task)

    def join_tasks(self):
        self._rotation_queue.join()

    def _get_stream_object(self, filename):
        try:
            return StreamObject(self.s3, self._bucket.name, filename, self._stream_buffer_queue)

        except Exception:
            raise RuntimeError('Failed to open new S3 stream object')

    def _rotate_chunk(self, async=True):

        assert self._current_object, "Stream object not found"

        part_num = self._current_object.chunk_count + 1
        part = self._current_object.uploader.Part(part_num)
        buffer = self._current_object.buffer
        self._current_object.buffer = BytesIO()
        buffer.seek(0)
        if async:
            self._current_object.add_task(Task(self._upload_part, self._current_object, part, part_num, buffer))
        else:
            self._upload_part(self._current_object, part, part_num, buffer)

        self._current_object.chunk_count += 1

    @staticmethod
    def _upload_part(s3_object, part, part_num, buffer):
        upload = part.upload(Body=buffer)
        s3_object.parts.append({'ETag': upload['ETag'], 'PartNumber': part_num})

    def _rotate_file(self):

        if self._current_object.buffer.tell() > 0:
            self._rotate_chunk()

        temp_object = self._current_object
        self.add_task(Task(self._close_stream, stream_object=temp_object))
        new_filename = "{}_{}".format(self.key, int(datetime.utcnow().strftime('%s')))
        self.start_time = int(datetime.utcnow().strftime('%s'))
        self._current_object = self._get_stream_object(new_filename)

    @staticmethod
    def _close_stream(stream_object, callback=None, *args, **kwargs):
        stream_object.join_tasks()
        if stream_object.chunk_count > 0:
            stream_object.uploader.complete(MultipartUpload={'Parts': stream_object.parts})
        else:
            stream_object.uploader.abort()

        if callback and callable(callback):
            callback(*args, **kwargs)

    def close(self, *args, **kwargs):

        if self._current_object.buffer.tell() > 0:
            self._rotate_chunk(async=False)
        self.join_tasks()

        # Stop the worker threads
        for _ in range(len(self.workers)):
            self._rotation_queue.put(-1)

        for _ in range(len(self.stream_bg_workers)):
            self._stream_buffer_queue.put(-1)

        self._close_stream(self._current_object)
        self._is_open = False

    @property
    def closed(self):
        return not self._is_open

    @property
    def writable(self, *args, **kwargs):
        return True

    def tell(self, *args, **kwargs):
        return self._current_object.byte_count

    def write(self, *args, **kwargs):
        s = args[0]
        self._current_object.buffer.write(s.encode(self.encoder))
        self._current_object.byte_count = self._current_object.byte_count + len(s)

        if self._current_object.buffer.tell() > self.chunk_size:
            self._rotate_chunk()

        if (self.max_file_size_bytes and self._current_object.byte_count > self.max_file_size_bytes) or (
                self.max_file_log_time and int(
            datetime.utcnow().strftime('%s')) - self.start_time > self.max_file_log_time):
            self._rotate_file()

        return len(s)


class S3Handler(StreamHandler):
    """
    A Logging handler class that streams log records to S3 by chunks
    """

    def __init__(self, file_path, bucket, key_id, secret, chunk_size=DEFAULT_CHUNK_SIZE,
                 time_rotation=DEFAULT_ROTATION_TIME_SECS, max_file_size_bytes=MAX_FILE_SIZE_BYTES, encoder='utf-8'):
        """

        :param file_path: The path of the S3 object
        :param bucket: The id of the S3 bucket
        :param key_id: Authentication key
        :param secret: Authentication secret
        :param chunk_size: Size of a chunk in the multipart upload in bytes - default 5MB
        :param time_rotation: Interval in seconds to rotate the file by - default 12 hours
        :param max_file_size_bytes: Maximum file size in bytes before rotation - default 100MB
        :param encoder: default utf-8
        """
        self.bucket = bucket
        self.secret = secret
        self.key_id = key_id
        self.stream = S3Streamer(self.bucket, self.key_id, self.secret, file_path, chunk_size, time_rotation,
                                 max_file_size_bytes, encoder)

        # Make sure we gracefully clear the buffers and upload the missing parts before exiting
        signal.signal(signal.SIGTERM, self.close)
        signal.signal(signal.SIGINT, self.close)
        signal.signal(signal.SIGQUIT, self.close)
        atexit.register(self.close)

        StreamHandler.__init__(self, self.stream)

    def close(self, *args, **kwargs):
        """
        Closes the stream
        """
        self.acquire()
        try:
            if self.stream:
                try:
                    self.flush()
                finally:
                    stream = self.stream
                    self.stream = None
                    if hasattr(stream, "close"):
                        stream.close(*args, **kwargs)
        finally:
            self.release()
