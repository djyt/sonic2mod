import os
from typing import Literal
from tables import ModNote, PERIOD_TABLE

# File format information: https://www.exotica.org.uk/wiki/Protracker
BYTE_ORDER: Literal["little", "big"] = "big"


class ModSample:
    __name: str
    length: int
    __finetune: int
    __volume: int
    repeat: int
    repeat_length: int
    data: bytes

    def __init__(self, name):
        self.__name = name[0:21]
        self.length = 0
        self.__finetune = 0
        self.__volume = 0
        self.repeat = 0
        self.repeat_length = 1
        self.data = bytes(0)

    def set_finetune(self, v: int):
        if v < -8 or v > 7:
            print("Warning: Fine Tune " + str(v) + " is invalid.")
            return
        self.__finetune = v & 0xf

    def set_volume(self, v):
        if v < 0 or v > 64:
            print("Warning: Volume " + str(v) + " is invalid.")
            return
        self.__volume = int(v)

    def get_name(self): return self.__name.ljust(21, ' ')
    def get_name_bytes(self): return bytearray(self.get_name(), 'utf-8') + b'\x00'
    def get_bytes(self):
        output = bytearray()
        output += self.get_name_bytes()
        output += self.length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self.__finetune.to_bytes(1, byteorder=BYTE_ORDER, signed=True)
        output += self.__volume.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        output += self.repeat_length.to_bytes(2, byteorder=BYTE_ORDER, signed=False)
        return output


class ModPattern:
    __data: bytearray
    __plen: int

    def __init__(self, chan: int = 4):
        size = chan * 256  # chan * 4 bytes * 64 rows
        self.__data = bytearray(size)
        self.__plen = size

    def get_bytes(self): return self.__data

    def set_entry(self, index: int, value: int):
        if index < 0 or index > self.__plen - 1:
            print("Invalid index " + str(index))
            return
        self.__data[index] = value

    def get_entry(self, index: int):
        if index < 0 or index > self.__plen - 1:
            print("Invalid index " + str(index))
            return 0
        return self.__data[index]


class ModFile:
    FORMAT_TABLE = {
        4: "M.K.",
        8: "8CHN",
        10: "10CH",
        12: "12CH",
        14: "14CH",
        16: "16CH",
    }

    MAX_POSITIONS: int = 127

    def __init__(self, channels=10):
        self.CHANNELS = channels
        self.MOD_FORMAT = self.FORMAT_TABLE.get(channels, "M.K.").encode("utf-8")
        self.SONG_LENGTH = self.MAX_POSITIONS
        self.__name = "untitled"
        self.samples = [ModSample("unused " + str(i)) for i in range(31)]
        self.positions = 1
        self.position_list = bytearray(self.MAX_POSITIONS + 1)
        self.patterns = [ModPattern(self.CHANNELS)]
        self.__active_pattern = 0
        self.__chan = 0
        self.__row = 0
        self.__inst = 0

    def set_name(self, name: str): self.__name = name[0:19]
    def get_name(self): return self.__name.ljust(19, ' ')
    def get_name_bytes(self): return bytearray(self.get_name(), 'utf-8') + b'\x00'

    def get_bytes(self):
        output = bytearray()
        output += self.get_name_bytes()
        for sample in self.samples:
            output += sample.get_bytes()
        output += self.positions.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.SONG_LENGTH.to_bytes(1, byteorder=BYTE_ORDER, signed=False)
        output += self.position_list
        output += self.MOD_FORMAT
        for pattern in self.patterns:
            output += pattern.get_bytes()
        for sample in self.samples:
            output += sample.data
        return output

    def get_index(self):
        return (self.__chan * 4) + (self.__row * (self.CHANNELS * 4))

    def set_channel(self, chan: int):
        if chan < 0 or chan > self.CHANNELS - 1:
            print("Error: Channel " + str(chan) + " is invalid.")
        else:
            self.__chan = chan

    def set_row(self, row: int):
        if row < 0 or row > 63:
            print("Error: Row " + str(row) + " is invalid.")
        else:
            self.__row = row

    def inc_row(self, rows: int):
        pattern_len = len(self.patterns)
        for i in range(rows):
            self.__row += 1
            if self.__row > 63:
                self.__row = 0
                if self.__active_pattern + 1 >= pattern_len:
                    self.add_patterns(1)
                self.set_active_pattern(self.__active_pattern + 1)

    def set_inst(self, i: int):
        if i < 0 or i > 0x1f:
            print("Error: Instrument " + str(i) + " is invalid.")
            return
        self.__inst = i

    def set_note(self, n: ModNote, inst: int = None):
        if inst is None:
            inst = self.__inst

        if inst < 0 or inst > 0x1f:
            print("Error: Instrument " + str(inst) + " is invalid.")
            return
        note_period = PERIOD_TABLE[n.value]
        index = self.get_index()
        value = (note_period << 16) + ((inst & 0xf) << 12) + ((inst >> 4) << 28)

        pattern = self.patterns[self.__active_pattern]
        pattern.set_entry(index + 0, (value >> 24) & 0xff)
        pattern.set_entry(index + 1, (value >> 16) & 0xff)

        # Retain effect parameter
        index_2 = pattern.get_entry(index + 2) & 0xf
        pattern.set_entry(index + 2, index_2 + ((value >> 8) & 0xff))

    def get_active_pattern(self):
        return self.__active_pattern

    def set_active_pattern(self, pattern: int):
        if pattern < 0 or pattern > self.MAX_POSITIONS:
            print("Error: Pattern " + str(pattern) + " does not exist.")
        elif pattern >= len(self.patterns):
            self.add_patterns(pattern - len(self.patterns) + 1)
            self.__active_pattern = pattern
        else:
            self.__active_pattern = pattern

    def add_patterns(self, number_to_add: int):
        if number_to_add <= 0: return

        length = len(self.patterns)
        add = number_to_add + length
        if add > self.MAX_POSITIONS:
            print("Warning: Exceeded 127 patterns. Truncating to 127.")
            number_to_add = self.MAX_POSITIONS - length
        for i in range(number_to_add):
            self.patterns.append(ModPattern(self.CHANNELS))
            self.position_list[length + i] = length + i
        self.positions += number_to_add

    def set_bpm(self, bpm: int):
        if bpm < 32 or bpm > 255:
            print("Warning: BPM out of range (32-255) " + str(bpm))
            return
        pattern = self.patterns[0]
        pattern.set_entry(2, 0xf + (pattern.get_entry(2) & 0xf0))
        pattern.set_entry(3, bpm)

    def set_speed(self, speed: int):
        if speed < 0 or speed > 31:
            print("Warning: Speed out of range (0-31) " + str(speed))
            return
        # Set speed on channel 1, row 0
        index = 1 * 4
        pattern = self.patterns[0]
        pattern.set_entry(index + 2, (pattern.get_entry(index + 2) & 0xf0) | 0xf)
        pattern.set_entry(index + 3, speed)

    def set_volume(self, vol: int):
        if vol < 0 or vol > 0x40:
            print("Warning: Volume out of range (0-64) " + str(vol))
            return
        index = self.get_index()
        pattern = self.patterns[self.__active_pattern]
        pattern.set_entry(index + 2, 0xc + (pattern.get_entry(index + 2) & 0xf0))
        pattern.set_entry(index + 3, vol)

    def set_effect(self, effect: int, param: int):
        """Set a generic effect on the current channel/row.

        Args:
            effect: Effect number (0x0-0xF)
            param: Effect parameter (0x00-0xFF)
        """
        index = self.get_index()
        pattern = self.patterns[self.__active_pattern]
        pattern.set_entry(index + 2, (pattern.get_entry(index + 2) & 0xf0) + (effect & 0xf))
        pattern.set_entry(index + 3, param & 0xff)

    def set_position_jump(self, position: int):
        """Set Bxx position jump effect on the current channel/row.

        Args:
            position: Position to jump to (0-127)
        """
        self.set_effect(0xB, position & 0x7f)

    def add_samples(self, working_dir: str, sample_list: list):
        if sample_list is None: return
        for entry in sample_list:
            sample_index: int = entry[0]
            filename: str = entry[1]
            vol: int = entry[2]
            try:
                finetune: int = entry[3]
            except IndexError:
                finetune: int = 0
            full_path = os.path.join(working_dir, filename)

            if sample_index < 1 or sample_index > 0x1f:
                print("Error: Instrument " + str(sample_index) + " is invalid.")
                continue

            if vol < 0 or vol > 0x40:
                print("Warning: Volume out of range (0-64). Setting to 64 " + str(vol))
                vol = 64

            if not os.path.exists(full_path):
                print(f"Warning: Sample file not found: {full_path}")
                continue

            with open(full_path, "rb") as file:
                data = file.read()
                if len(data) > 0xffff:
                    print(f"Error: {filename} is larger than 64K!")
                    continue

                sample = ModSample(filename)
                sample.data = data
                sample.set_volume(vol)
                sample.length = int(len(data) / 2)
                sample.set_finetune(finetune)
                self.samples[sample_index - 1] = sample

    def create_placeholder_samples(self, count=10):
        """Create minimal placeholder samples for instruments 1-count.
        Users can replace these with real samples later.
        """
        for i in range(1, min(count + 1, 32)):
            sample = ModSample(f"Placeholder {i}")
            # Create a tiny 2-byte silent sample
            sample.data = bytes([0, 0])
            sample.length = 1  # 1 word = 2 bytes
            sample.set_volume(64)
            self.samples[i - 1] = sample
