from pyarrow.dataset import dataset as pyarrow_dataset
import pyarrow.parquet as pq
import re
import json
from copy import deepcopy
from fsspec.implementations.local import LocalFileSystem


class DeltaTable:
    def __init__(self, path, file_system=None):

        self.path = path
        if file_system is None:
            file_system = LocalFileSystem()
        self.filesystem = file_system
        self._as_newest_version()

        # The PyArrow Dataset is exposed by a factory class,
        # which makes it hard to inherit from it directly.
        # Instead we will just have the dataset as an attribute and expose the important methods.
        self.pyarrow_dataset = pyarrow_dataset(
            source=list(self.files), filesystem=self.filesystem
        )

    def _is_delta_table(self):
        return self.filesystem.exists(f"{self.path}/_delta_log/{0:020}.json")

    def _apply_from_checkpoint(self, checkpoint_version: int):

        # reset file set, and checkpoint version
        self.files = set()
        self.checkpoint = checkpoint_version

        if self.checkpoint == 0:
            return

        # read latest checkpoint
        with self.filesystem.open(
            f"{self.path}/_delta_log/{self.checkpoint:020}.checkpoint.parquet"
        ) as checkpoint_file:
            checkpoint = pq.read_table(checkpoint_file).to_pandas()

            for i, row in checkpoint.iterrows():
                added_file = row["add"]["path"] if row["add"] else None
                if added_file:
                    self.files.add(f"{self.path}/{added_file}")

    def _apply_partial_logs(self, version: int):
        # Checkpoints are created every 10 transactions,
        # so we need to find all log files with version
        # up to 9 higher than checkpoint.
        # Effectively, this means that we can just create a
        # wild card for the first decimal of the checkpoint version

        log_files = self.filesystem.glob(
            f"{self.path}/_delta_log/{self.checkpoint//10:019}*.json"
        )
        # sort the log files, so we are sure we get the correct order
        log_files = sorted(log_files)
        for log_file in log_files:

            # Get version from log name
            log_version = re.findall(r"(\d{20})", log_file)[0]
            self.version = int(log_version)

            # Download log file
            log = self.filesystem.cat(log_file)
            for line in log.split():
                meta_data = json.loads(line)
                # Log contains other stuff, but we are only
                # interested in the add or remove entries
                if "add" in meta_data.keys():
                    self.files.add(f"{self.path}/{meta_data['add']['path']}")
                if "remove" in meta_data.keys():
                    remove_file = meta_data["remove"]["path"]
                    # To handle 0 checkpoints, we might read the log file with
                    # same version as checkpoint. this means that we try to
                    # remove a file that belongs to an ealier version,
                    # which we don't have in the list
                    if remove_file in self.files:
                        self.files.remove(f"{self.path}/{remove_file}")
            # Stop if we have reatched the desired version
            if self.version == version:
                break

    def _as_newest_version(self):
        # Try to get the latest checkpoint info
        try:
            # get latest checkpoint version
            checkpoint_info = self.filesystem.cat(
                f"{self.path}/_delta_log/_last_checkpoint"
            )
            checkpoint_info = json.loads(checkpoint_info)
            self._apply_from_checkpoint(checkpoint_info["version"])

        except FileNotFoundError:
            pass

        # apply remaining versions. This can be a maximum of 9 versions.
        # we will just break when we don't find any newer logs
        self._apply_partial_logs(version=self.checkpoint + 9)

    def to_table(self, columns=None, filter=None, batch_size=1e9, **kwargs):
        """
        https://arrow.apache.org/docs/python/generated/pyarrow.dataset.FileSystemDataset.html#pyarrow.dataset.FileSystemDataset.scan
        """
        return self.pyarrow_dataset.to_table(
            columns=columns, filter=filter, batch_size=batch_size, **kwargs
        )

    def to_pandas(self, columns=None, filter=None, batch_size=1e9, **kwargs):
        """
        # https://arrow.apache.org/docs/python/generated/pyarrow.Table.html?highlight=to_pandas#pyarrow.Table.to_pandas
        """
        return self.to_table(
            columns=columns, filter=filter, batch_size=batch_size
        ).to_pandas(**kwargs)

    def as_version(self, version: int, inplace=True):
        """
        Find the files for a specific version of the table.

        Parameters:
        ----------
        version: (int)
            The table version number that should be loaded

        inplace: (Bool)
            Specify wether the object should be modified inplace or not.
            If `True`, the current object will be modified.
            if `False`, a new instance of the `DeltaTable` will be returned with the given version.

        Returns:
        -------
        dr : (DeltaTable)
            Delta table that has parsed the log files for the specific version
        """
        nearest_checkpoint = version // 10
        if inplace:
            self._apply_from_checkpoint(nearest_checkpoint)
            self._apply_partial_logs(version=version)
            self.pyarrow_dataset = pyarrow_dataset(
                source=list(self.files), filesystem=self.filesystem
            )
            return self

        deltaTable = deepcopy(self)
        deltaTable._apply_from_checkpoint(nearest_checkpoint)
        deltaTable._apply_partial_logs(version=version)

        return deltaTable
