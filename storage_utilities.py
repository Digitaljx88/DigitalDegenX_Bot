import json
import os
import fcntl

class SafeStorage:
    def __init__(self, file_path):
        self.file_path = file_path

    def _lock_file(self, file):
        fcntl.flock(file, fcntl.LOCK_EX)

    def _unlock_file(self, file):
        fcntl.flock(file, fcntl.LOCK_UN)

    def write(self, data):
        with open(self.file_path, 'w') as file:
            self._lock_file(file)
            json.dump(data, file)
            self._unlock_file(file)

    def read(self):
        if not os.path.exists(self.file_path):
            return {}

        with open(self.file_path, 'r') as file:
            self._lock_file(file)
            data = json.load(file)
            self._unlock_file(file)
            return data
