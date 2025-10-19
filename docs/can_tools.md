```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
 Copyright (c) 2025 iota2 (iota2 Engineering Tools)
 Licensed under the MIT License. See LICENSE file in the project root for details.
```

# CAN Tools

## `candump` - Viewing `CAN` Packets

Assuming the device is recognized as `can0`:

```bash
candump can0
       can0  701   [1]  00
       can0  181   [8]  00 00 00 00 00 00 00 00
       can0  181   [4]  00 00 00 00
       can0  381   [6]  00 00 00 00 00 00
       can0  701   [1]  05
       can0  181   [8]  00 00 00 00 00 00 00 00
       can0  181   [4]  00 00 00 00
       can0  381   [6]  00 00 00 00 00 00
       can0  181   [8]  00 00 00 00 00 00 00 00
       can0  181   [4]  00 00 00 00
       can0  381   [6]  00 00 00 00 00 00
```

## `cansend` - Sending data over `CAN`

```bash
cansend - send CAN-frames via CAN_RAW sockets.
  Usage:
      ./cansend <device> <can_frame>.  <can_frame>:
      <can_id>#{R|data}
      for CAN 2.0 frames
      <can_id>##<flags>{data}
      for CAN FD frames
      <can_id>:  can have 3 (SFF) or 8 (EFF) hex chars
      {data}:              has 0..8 (0..64 CAN FD) ASCII hex-values (optionally separated by '.')
      <flags>:              a single ASCII Hex value (0 .. F) which defines canfd_frame.flags

      Examples:
        5A1#11.2233.44556677.88
        123#DEADBEEF
        5AA#
        123##1
        213##311
        1F334455#1122334455667788
        123#R for remote transmission request.
```

## `cansniffer` - `CAN` Content visualizer

```bash
cansniffer
    cansniffer - volatile CAN content visualizer.

    Usage: cansniffer [can-interface]
    Options:
         -q          (quiet - all IDs deactivated)
         -r <name>   (read sniffset.name from file)
         -e          (fix extended frame format output - no auto detect)
         -b          (start with binary mode)
         -8          (start with binary mode - for EFF on 80 chars)
         -B          (start with binary mode with gap - exceeds 80 chars!)
         -c          (color changes)
         -t <time>   (timeout for ID display [x10ms] default: 500, 0 = OFF)
         -h <time>   (hold marker on changes [x10ms] default: 100)
         -l <time>   (loop time (display) [x10ms] default: 20)
         -?          (print this help text)
    Use interface name 'any' to receive from all can-interfaces.

    commands that can be entered at runtime:
     q<ENTER>        - quit
     b<ENTER>        - toggle binary / HEX-ASCII output
     8<ENTER>        - toggle binary / HEX-ASCII output (small for EFF on 80 chars)
     B<ENTER>        - toggle binary with gap / HEX-ASCII output (exceeds 80 chars!)
     c<ENTER>        - toggle color mode
     <SPACE><ENTER>  - force a clear screen
     #<ENTER>        - notch currently marked/changed bits (can be used repeatedly)
     *<ENTER>        - clear notched marked
     rMYNAME<ENTER>  - read settings file (filter/notch)
     wMYNAME<ENTER>  - write settings file (filter/notch)
     a<ENTER>        - enable 'a'll SFF CAN-IDs to sniff
     n<ENTER>        - enable 'n'one SFF CAN-IDs to sniff
     A<ENTER>        - enable 'A'll EFF CAN-IDs to sniff
     N<ENTER>        - enable 'N'one EFF CAN-IDs to sniff
     +FILTER<ENTER>  - add CAN-IDs to sniff
     -FILTER<ENTER>  - remove CAN-IDs to sniff

    FILTER can be a single CAN-ID or a CAN-ID/Bitmask:

     single SFF 11 bit IDs:
      +1F5<ENTER>               - add SFF CAN-ID 0x1F5
      -42E<ENTER>               - remove SFF CAN-ID 0x42E

     single EFF 29 bit IDs:
      +18FEDF55<ENTER>          - add EFF CAN-ID 0x18FEDF55
      -00000090<ENTER>          - remove EFF CAN-ID 0x00000090

     CAN-ID/Bitmask SFF:
      -42E7FF<ENTER>            - remove SFF CAN-ID 0x42E (using Bitmask)
      -500700<ENTER>            - remove SFF CAN-IDs 0x500 - 0x5FF
      +400600<ENTER>            - add SFF CAN-IDs 0x400 - 0x5FF
      +000000<ENTER>            - add all SFF CAN-IDs
      -000000<ENTER>            - remove all SFF CAN-IDs

     CAN-ID/Bitmask EFF:
      -0000000000000000<ENTER>  - remove all EFF CAN-IDs
      +12345678000000FF<ENTER>  - add EFF CAN IDs xxxxxx78
      +0000000000000000<ENTER>  - add all EFF CAN-IDs

    if (id & filter) == (sniff-id & filter) the action (+/-) is performed,
    which is quite easy when the filter is 000 resp. 00000000 for EFF.
```

## `can_viewer` - `CAN` viewer terminal application

```bash
can_viewer
Usage: python -m can.viewer [-c CHANNEL] [-i {canalystii,cantact,etas,gs_usb,iscan,ixxat,kvaser,neousys,neovi,nican,nixnet,pcan,robotell,seeedstudio,serial,slcan,socketcan,socketcand,systec,udp_multicast,usb2can,vector,virtual}]
                            [-b BITRATE] [--fd] [--data-bitrate DATA_BITRATE] [--timing ('TIMING_ARG',)] [--filter ('{<can_id>:<can_mask>,<can_id>~<can_mask>}',)] [--bus-kwargs ('BUS_KWARG',)] [-h] [--version]
                            [-d ('{<id>:<format>,<id>:<format>:<scaling1>:...:<scalingN>,file.txt}',)] [-v]

A simple CAN viewer terminal application written in Python

Bus arguments:
  -c, --channel CHANNEL
                        Most backend interfaces require some sort of channel. For example with the serial interface the channel might be a rfcomm device: "/dev/rfcomm0". With the socketcan interface valid channel examples include:
                        "can0", "vcan0".
  -i, --interface {canalystii,cantact,etas,gs_usb,iscan,ixxat,kvaser,neousys,neovi,nican,nixnet,pcan,robotell,seeedstudio,serial,slcan,socketcan,socketcand,systec,udp_multicast,usb2can,vector,virtual}
                        Specify the backend CAN interface to use. If left blank, fall back to reading from configuration files.
  -b, --bitrate BITRATE
                        Bitrate to use for the CAN bus.
  --fd                  Activate CAN-FD support
  --data-bitrate DATA_BITRATE
                        Bitrate to use for the data phase in case of CAN-FD.
  --timing ('TIMING_ARG',)
                        Configure bit rate and bit timing. For example, use `--timing f_clock=8_000_000 tseg1=5 tseg2=2 sjw=2 brp=2 nof_samples=1` for classical CAN or `--timing f_clock=80_000_000 nom_tseg1=119 nom_tseg2=40
                        nom_sjw=40 nom_brp=1 data_tseg1=29 data_tseg2=10 data_sjw=10 data_brp=1` for CAN FD. Check the python-can documentation to verify whether your CAN interface supports the `timing` argument.
  --filter ('{<can_id>:<can_mask>,<can_id>~<can_mask>}',)
                        Space separated CAN filters for the given CAN interface:
                              <can_id>:<can_mask> (matches when <received_can_id> & mask == can_id & mask)
                              <can_id>~<can_mask> (matches when <received_can_id> & mask != can_id & mask)
                        Fx to show only frames with ID 0x100 to 0x103 and 0x200 to 0x20F:
                              python -m can.viewer --filter 100:7FC 200:7F0
                        Note that the ID and mask are always interpreted as hex values
  --bus-kwargs ('BUS_KWARG',)
                        Pass keyword arguments down to the instantiation of the bus class. For example, `-i vector -c 1 --bus-kwargs app_name=MyCanApp serial=1234` is equivalent to opening the bus with `can.Bus('vector', channel=1,
                        app_name='MyCanApp', serial=1234)

Optional arguments:
  -h, --help            Show this help message and exit
  --version             Show program's version number and exit
  -d, --decode ('{<id>:<format>,<id>:<format>:<scaling1>:...:<scalingN>,file.txt}',)
                        Specify how to convert the raw bytes into real values.
                        The ID of the frame is given as the first argument and the format as the second.
                        The Python struct package is used to unpack the received data
                        where the format characters have the following meaning:
                              < = little-endian, > = big-endian
                              x = pad byte
                              c = char
                              ? = bool
                              b = int8_t, B = uint8_t
                              h = int16, H = uint16
                              l = int32_t, L = uint32_t
                              q = int64_t, Q = uint64_t
                              f = float (32-bits), d = double (64-bits)
                        Fx to convert six bytes with ID 0x100 into uint8_t, uint16 and uint32_t:
                          $ python -m can.viewer -d "100:<BHL"
                        Note that the IDs are always interpreted as hex values.
                        An optional conversion from integers to real units can be given
                        as additional arguments. In order to convert from raw integer
                        values the values are divided with the corresponding scaling value,
                        similarly the values are multiplied by the scaling value in order
                        to convert from real units to raw integer values.
                        Fx lets say the uint8_t needs no conversion, but the uint16 and the uint32_t
                        needs to be divided by 10 and 100 respectively:
                          $ python -m can.viewer -d "101:<BHL:1:10.0:100.0"
                        Be aware that integer division is performed if the scaling value is an integer.
                        Multiple arguments are separated by spaces:
                          $ python -m can.viewer -d "100:<BHL" "101:<BHL:1:10.0:100.0"
                        Alternatively a file containing the conversion strings separated by new lines
                        can be given as input:
                          $ cat file.txt
                              100:<BHL
                              101:<BHL:1:10.0:100.0
                          $ python -m can.viewer -d file.txt
  -v                    How much information do you want to see at the command line? You can add several of these e.g., -vv is DEBUG

Shortcuts:
        +---------+-------------------------------+
        |   Key   |       Description             |
        +---------+-------------------------------+
        | ESQ/q   | Exit the viewer               |
        | c       | Clear the stored frames       |
        | s       | Sort the stored frames        |
        | h       | Toggle highlight byte changes |
        | SPACE   | Pause the viewer              |
        | UP/DOWN | Scroll the viewer             |
        +---------+-------------------------------+
```
