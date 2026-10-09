"""Register map of the DDSU666, transcribed from table 9 of the operation manual.

Every row of the manual is listed, RESERVED rows included, so the GUI can show
the whole table. Addresses are register numbers as written in the manual (0006H).
"""

from __future__ import annotations

from dataclasses import dataclass

INT16 = "int16"      # "16-bit with symbols"
FLOAT32 = "float32"  # "single precision floating decimal", 2 registers

# Value notes printed under table 9.
PROTOCOL_CHOICES = ((2, "Modbus RTU"), (1, "DL/T 645-2007"))
BAUD_CHOICES = ((1, "2400 bps"), (2, "4800 bps"), (3, "9600 bps"))
BAUD_RATES = {1: 2400, 2: 4800, 3: 9600}

# Frame formats selectable with the front-panel button (figure 3). They are not
# registers: the PC side has to match whatever the meter is set to.
FRAME_FORMATS = ("8N1", "8N2", "8E1", "8O1")
# Section 4.3 lists 1200 bps as well, although the BAud register only defines 1..3.
LINE_BAUDS = (9600, 4800, 2400, 1200)


@dataclass(frozen=True)
class Register:
    address: int
    code: str
    name: str            # Vietnamese description shown in the GUI
    manual: str          # wording of the manual
    kind: str = INT16
    access: str = "R"    # "R", "R/W" or "" (RESERVED rows of table 9 carry no attribute)
    unit: str = ""
    choices: tuple[tuple[int, str], ...] = ()

    @property
    def words(self) -> int:
        return 2 if self.kind == FLOAT32 else 1

    @property
    def writable(self) -> bool:
        return self.access == "R/W"

    @property
    def reserved(self) -> bool:
        return self.code == "RESERVED"

    def choice_label(self, value: int) -> str | None:
        for choice, label in self.choices:
            if choice == value:
                return label
        return None


def _reserved(address: int, kind: str = INT16, access: str = "") -> Register:
    return Register(address, "RESERVED", "Dự phòng", "RESERVED", kind, access)


UCODE = Register(0x0000, "UCode", "Mật khẩu lập trình", "Programming password codE", access="R/W")
REV = Register(0x0001, "REV.", "Phiên bản (đọc ra là số phiên bản)",
               "Reserved, actual read is the version number")
CLR_E = Register(0x0002, "ClrE", "Xóa điện năng CLr.E (ghi 1 = xóa)",
                 "Electric energy zero clearing CLr.E(1:zero clearing)", access="R/W")
CHANGE_PROTOCOL = Register(0x0005, "ChangeProtocol", "Chuyển giao thức: 2 = Modbus RTU, 1 = DL/T 645-2007",
                           "Protocol changing-over", access="R/W", choices=PROTOCOL_CHOICES)
ADDR = Register(0x0006, "Addr", "Địa chỉ truyền thông", "Communication address Addr", access="R/W")
METER_TYPE = Register(0x000B, "Meter type", "Loại đồng hồ", "Meter type")
BAUD = Register(0x000C, "BAud", "Tốc độ baud: 1 = 2400, 2 = 4800, 3 = 9600",
                "Communication baud rate bAud", access="R/W", choices=BAUD_CHOICES)

PARAMETERS = (
    UCODE,
    REV,
    CLR_E,
    _reserved(0x0003),
    _reserved(0x0004),
    CHANGE_PROTOCOL,
    ADDR,
    _reserved(0x0007),
    _reserved(0x0008),
    _reserved(0x0009),
    _reserved(0x000A),
    METER_TYPE,
    BAUD,
    _reserved(0x000D),
    _reserved(0x000E),
    _reserved(0x000F),
    _reserved(0x0010),
)

# "Electric quantity of the secondary side" (on DDSU666-CT the CT ratio is not applied).
VOLTAGE = Register(0x2000, "U", "Điện áp", "Voltage", FLOAT32, "R", "V")
CURRENT = Register(0x2002, "I", "Dòng điện", "Current", FLOAT32, "R", "A")
ACTIVE_POWER = Register(0x2004, "P", "Công suất tác dụng", "Conjunction active power, the unit is KW",
                        FLOAT32, "R", "kW")
REACTIVE_POWER = Register(0x2006, "Q", "Công suất phản kháng", "Conjunction reactive power, the unit is Kvar",
                          FLOAT32, "R", "kvar")
POWER_FACTOR = Register(0x200A, "PF", "Hệ số công suất", "Conjunction power factor", FLOAT32, "R")
FREQUENCY = Register(0x200E, "Freq", "Tần số", "Frequency", FLOAT32, "R", "Hz")

MEASUREMENTS = (
    VOLTAGE,
    CURRENT,
    ACTIVE_POWER,
    REACTIVE_POWER,
    _reserved(0x2008, FLOAT32, "R"),
    POWER_FACTOR,
    _reserved(0x200C, FLOAT32, "R"),
    FREQUENCY,
    _reserved(0x2010, FLOAT32, "R"),
)

# "Electrical data of the secondary side"; the LCD shows these in kWh.
IMPORT_ENERGY = Register(0x4000, "Ep", "Điện năng tác dụng thuận (nhận, Imp)", "Active in electricity",
                         FLOAT32, "R", "kWh")
EXPORT_ENERGY = Register(0x400A, "-Ep", "Điện năng tác dụng ngược (phát, Exp)", "Reverse in electricity",
                         FLOAT32, "R", "kWh")

ENERGY = (IMPORT_ENERGY, EXPORT_ENERGY)


@dataclass(frozen=True)
class Block:
    """A contiguous span the client first tries to read with a single request."""

    start: int
    count: int
    registers: tuple[Register, ...]


PARAMETER_BLOCK = Block(0x0000, 0x11, PARAMETERS)
MEASUREMENT_BLOCK = Block(0x2000, 0x12, MEASUREMENTS)
ENERGY_BLOCK = Block(0x4000, 0x0C, ENERGY)

ALL_REGISTERS = PARAMETERS + MEASUREMENTS + ENERGY
BY_ADDRESS = {register.address: register for register in ALL_REGISTERS}
