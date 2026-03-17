import os
from typing import ClassVar, Literal

from .tables import PERIOD_TABLE, ModNote

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
    FORMAT_TABLE: ClassVar[dict[int, str]] = {
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
        self.samples = [ModSample("") for _ in range(31)]
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
        for _ in range(rows):
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

    def set_note(self, n: ModNote, inst: int | None = None):
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

    def trim_to_pattern(self, last_pattern: int) -> None:
        """Remove patterns after last_pattern (unreachable once the loop-point Bxx is set).

        Called after _set_loop_point to discard any trailing blank patterns that
        apply_pattern_breaks may create when the body doesn't divide evenly into
        64-row chunks.
        """
        keep = last_pattern + 1
        if len(self.patterns) <= keep:
            return
        del self.patterns[keep:]
        self.positions = keep
        for i in range(keep):
            self.position_list[i] = i
        for i in range(keep, self.MAX_POSITIONS + 1):
            self.position_list[i] = 0

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


def apply_pattern_breaks(mod: ModFile, breaks: list) -> None:
    """Insert a Bxx position-jump and repack the pattern stream at the break row.

    The pattern data is treated as a continuous stream of rows.  For each
    (pattern_slot, row) in breaks (processed ascending):

      1. All rows after the break (pattern_slot rows row+1..63, then all rows
         of every subsequent pattern) are collected into a flat body stream.
      2. That body stream is repacked into patterns starting at pattern_slot+1,
         row 0 — so each new pattern is fully packed with 64 rows of body data.
         One extra pattern is appended at the end if the body doesn't divide
         evenly into 64-row chunks.
      3. Rows row+1..63 of pattern_slot are cleared.
      4. Bxx → pattern_slot+1 is written at (pattern_slot, row) on the first
         free effect channel so the player skips the blank rows and enters the
         repacked stream at row 0.
      5. All Bxx effects that targeted patterns after the break are updated to
         reflect the body data's new position.  If the target data now starts
         at row > 0 within its new pattern, a Dxx (pattern-break, BCD-encoded)
         is written alongside the Bxx on a free adjacent channel so playback
         begins at the correct row.

    Raises ValueError if the required extra pattern would exceed MAX_POSITIONS.
    """
    stride = mod.CHANNELS * 4  # bytes per row

    def _to_bcd(n: int) -> int:
        """BCD-encode a row number: 32 → 0x32, 10 → 0x10."""
        return (n // 10) * 16 + (n % 10)

    for orig_slot, row in sorted(breaks):
        P = orig_slot  # pattern indices don't shift (we append, not insert)

        # body_start_flat: the old flat-row index of the first body row
        body_start_flat = P * 64 + row + 1

        # --- collect body as an immutable snapshot --------------------------
        body = bytearray()
        p_data = mod.patterns[P].get_bytes()
        for r in range(row + 1, 64):
            body += p_data[r * stride: r * stride + stride]
        N = len(mod.patterns) - 1
        for pi in range(P + 1, N + 1):
            body += bytes(mod.patterns[pi].get_bytes())

        body_rows      = len(body) // stride           # e.g. 1056 for GHZ
        n_pats_needed  = (body_rows + 63) // 64        # ceil division
        n_pats_avail   = N - P                         # patterns P+1..N
        n_new          = max(0, n_pats_needed - n_pats_avail)

        if mod.positions + n_new > mod.MAX_POSITIONS:
            raise ValueError(
                f"apply_pattern_breaks: {n_new} extra pattern(s) needed but "
                f"would exceed MAX_POSITIONS ({mod.MAX_POSITIONS})"
            )

        # append extra patterns and rebuild identity position list
        for _ in range(n_new):
            mod.patterns.append(ModPattern(mod.CHANNELS))
        mod.positions += n_new
        for i in range(mod.positions):
            mod.position_list[i] = i

        # --- clear rows row+1..63 from pattern P ----------------------------
        for r in range(row + 1, 64):
            for b in range(stride):
                mod.patterns[P].set_entry(r * stride + b, 0)

        # --- rewrite patterns P+1 .. P+n_pats_needed from body --------------
        pat_size = 64 * stride
        for pi_new in range(n_pats_needed):
            pat = mod.patterns[P + 1 + pi_new]
            for i in range(pat_size):
                pat.set_entry(i, 0)
            for r in range(64):
                br = pi_new * 64 + r
                if br < body_rows:
                    for b in range(stride):
                        pat.set_entry(r * stride + b, body[br * stride + b])

        # --- update Bxx effects that targeted patterns after the break -------
        # Old target T (pattern T, row 0) is now at:
        #   body_row   = T*64 - body_start_flat
        #   new_pat    = P+1 + body_row // 64
        #   new_row    = body_row % 64
        # If new_row != 0 a Dxx (BCD row) must accompany the Bxx so the
        # player starts at the right row within the new pattern.
        for pat in mod.patterns:
            pat_data = pat.get_bytes()
            for row_i in range(64):
                for chan_i in range(mod.CHANNELS):
                    idx = chan_i * 4 + row_i * stride
                    if (pat_data[idx + 2] & 0xF) != 0xB:
                        continue
                    target = pat_data[idx + 3]
                    if target <= P:
                        continue
                    old_flat = target * 64
                    if old_flat < body_start_flat:
                        continue
                    br          = old_flat - body_start_flat
                    new_pat_num = P + 1 + br // 64
                    new_row_num = br % 64
                    pat.set_entry(idx + 3, new_pat_num & 0x7F)
                    if new_row_num != 0:
                        bcd = _to_bcd(new_row_num)
                        for dxx_ch in range(mod.CHANNELS):
                            if dxx_ch == chan_i:
                                continue
                            didx = dxx_ch * 4 + row_i * stride
                            if (pat_data[didx + 2] & 0xF) == 0 and pat_data[didx + 3] == 0:
                                pat.set_entry(didx + 2, (pat_data[didx + 2] & 0xF0) | 0xD)
                                pat.set_entry(didx + 3, bcd)
                                break

        # --- write Bxx at (P, row) → P+1 on the first free channel ----------
        pat_data = mod.patterns[P].get_bytes()
        bxx_chan = 0
        for ch in range(mod.CHANNELS):
            idx = ch * 4 + row * stride
            if (pat_data[idx + 2] & 0xF) == 0 and pat_data[idx + 3] == 0:
                bxx_chan = ch
                break
        mod.set_active_pattern(P)
        mod.set_channel(bxx_chan)
        mod.set_row(row)
        mod.set_position_jump(P + 1)
