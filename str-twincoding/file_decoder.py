import filecmp
import os
import re
import time
import uuid
from collections import OrderedDict
import galois
import numpy as np
from tqdm import tqdm

from chunks import ChunksReader
from config import NodeType, load_file_config
from twin_coding import rs_generator_matrix
from util import open_output_file, assert_rs


# Decode a set of erasure-coded files supplied as a file map or from a storage-encoded directory.
# When a storage directory with config.json is provided the decoder will determine if a viable set
# of files is present and use them to decode the original file.  In either case at least k files of
# the same node type will be required.  Files are an array of encoded chunks of the original data,
# with the final chunk zero padded.
#
# Use `from_encoded_dir` for initializing a FileDecoder from an encoded dir with a config.json file.
#
# Note: We will parallelize this in a future update.  Lots of opportunity here to read chunks
# Note: in batches and perform decoding in parallel.
#
class FileDecoder(ChunksReader):
    def __init__(self,
                 node_type: NodeType,
                 file_map: dict[str, int] = None,
                 output_path: str = None,
                 overwrite: bool = False,
                 org_file_length: int = None  # original file length without encoder padding
                 ):

        assert_rs(node_type)
        self.k = node_type.k
        self.transpose = node_type.transpose
        self.node_type = node_type

        if file_map is None or len(file_map) != self.k:
            raise ValueError(f"file_map must be a dict of exactly {self.k} files.")

        self.output_path = output_path or f"decoded_{uuid.uuid4()}.dat"
        self.overwrite = overwrite
        self.org_file_length = org_file_length

        chunk_size = self.k  # individual columns of size k
        super().__init__(file_map=file_map, chunk_size=chunk_size)

    # Init a file decoder from an encoded file dir.  The dir must contain a config.json file and
    # at least k files of the same type.
    @staticmethod
    def from_encoded_dir(path: str, output_path: str = None, overwrite: bool = False):
        file_config = load_file_config(f'{path}/config.json')
        assert file_config.type0.k == file_config.type1.k, "Config node types must have the same k."
        recover_from_files = FileDecoder.map_files(path, k=file_config.type0.k)
        if os.path.basename(list(recover_from_files)[0]).startswith("type0_"):
            node_type = file_config.type0
        else:
            node_type = file_config.type1
            print("Decoding type 1 node: transposing.")

        return FileDecoder(
            node_type=node_type,
            file_map=recover_from_files,
            output_path=output_path,
            overwrite=overwrite,
            org_file_length=file_config.file_length
        )

    # Map the files in a file store encoded directory. At least k files of the same type must be present
    # to succeed. Returns a map of the first k files of either type found.
    @staticmethod
    def map_files(files_dir: str, k: int) -> dict[str, int]:
        type0_files, type1_files = {}, {}
        for filename in os.listdir(files_dir):
            match = re.match(r'type([01])_node(\d+).dat', filename)
            if not match:
                continue
            type_no, index_no = int(match.group(1)), int(match.group(2))
            files = type0_files if type_no == 0 else type1_files
            files[os.path.join(files_dir, filename)] = index_no

        if len(type0_files) >= k:
            return OrderedDict(sorted(type0_files.items(), key=lambda x: x[1])[:k])
        elif len(type1_files) >= k:
            return OrderedDict(sorted(type1_files.items(), key=lambda x: x[1])[:k])
        else:
            raise ValueError(
                f"Insufficient files in {files_dir} to recover: {len(type0_files)} type 0 files, "
                f"{len(type1_files)} type 1 files.")

    # Decode the file to the output path.
    def decode(self):
        with open_output_file(output_path=self.output_path, overwrite=self.overwrite) as out:
            k, n = self.node_type.k, self.node_type.n
            GF = galois.GF(2 ** 8)
            G = rs_generator_matrix(GF, k=k, n=n)
            g = G[:, self.files_indices]
            ginv = np.linalg.inv(g)

            # TODO: This will be parallelized
            start = time.time()
            with tqdm(total=self.num_chunks, desc='Decoding', unit='chunk') as pbar:
                for ci in range(self.num_chunks):
                    chunks = self.get_chunks(ci)

                    # Decode each chunk as a stack of column vectors forming a k x k matrix
                    matrix = np.hstack([chunk.reshape(-1, 1) for chunk in chunks])
                    decoded = GF(matrix) @ ginv
                    if self.transpose:
                        decoded = decoded.T
                    bytes = decoded.reshape(-1).tobytes()

                    # Trim the last chunk if it is padded
                    size = (ci + 1) * self.chunk_size * k
                    if size > self.org_file_length:
                        bytes = bytes[:self.org_file_length - size]

                    # Write the data to the output file
                    out.write(bytes)

                    # Progress bar
                    self.update_pbar(ci=ci, num_files=k, pbar=pbar, start=start)
        ...

    def close(self):
        [mm.close() for mm in self.mmaps]


if __name__ == '__main__':
    file = 'file_1KB.dat'
    encoded = f'{file}.encoded'
    recovered = 'recovered.dat'
    decoder = FileDecoder.from_encoded_dir(
        path=encoded,
        output_path=recovered,
        overwrite=True
    )
    decoder.decode()
    print("Passed" if filecmp.cmp(file, recovered) else "Failed")