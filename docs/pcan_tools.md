# PCAN Tools


## `receivetest` - Receiving data over `CAN`

- Display up to 100 (extended and standard) messages received from the 1 st CAN port of a USB interface connected to a CAN bus at 1 Mbit/s:

  ```bash
  receivetest -f=/dev/pcanusbfd32 -e -n=100
                receivetest Version "Release_20150611_n"  ([www.peak-system.com](http://www.peak-system.com/))
                ------- Copyright (C) 2004-2009 PEAK System-Technik GmbH ------
                receivetest comes with ABSOLUTELY NO WARRANTY.     This is free
                software  and you are welcome  to redistribute it under certain
                conditions.   For   details   see    attached   COPYING   file.
                receivetest: device node="/dev/pcanusbfd32"
                             Extended frames are accepted, init with 500 kbit/sec.
                receivetest: driver version = Release_20250213_n
                receivetest: LINUX_CAN_Read(): Resource temporarily unavailable
                receivetest: type            = usbfd
                             Serial Number   = 0x00000000
                             Device Number   = 1
                             count of reads  = 24702
                             count of writes = 0
                             count of errors = 0
                             count of irqs   = 207
                             last CAN status = 0x0000
                             last error      = 0
                             open paths      = 2
                             driver version  = Release_20250213_n
                receivetest: finished (11): 0 message(s) received
  ```

## `transmitest` - Tool for sending data over `CAN`

- Create `transmit.txt` file with CAN data frames:

  ```bash
  # standard messages
  m s 0x7FF 0 # a comment
  m s 0x7FB 8 0x88 0x99 0xAa 0xbB 0xCc 0xdD 0xEe 0xfF     # a comment

  # same as extended message
  m e 0x1FFFFFFB 8 0x88 0x99 0xAa 0xbB 0xCc 0xdD 0xEe 0xfF

  # same as remote and standard message
  r s 0x008 8 0x88 0x99 0xAa 0xbB 0xCc 0xdD 0xEe 0xfF

  # same as remote and extended
  r e 0x00000008 8 0x88 0x99 0xAa 0xbB 0xCc 0xdD 0xEe 0xfF # a comment
  ```

- Transmit 100 times all the CAN 2.0 frames described in transmit.txt to the 1st CAN port of a USB interface connected to a CAN bus at 1 Mbit/s:

  ```bash
  transmitest transmit.txt -f=/dev/pcanusbfd32 -b=0x14 -e -n=100
      transmitest Version "Release_20150610_n"  ([www.peak-system.com](http://www.peak-system.com/))
      ------- Copyright (C) 2004-2009 PEAK System-Technik GmbH ------
      transmitest comes with ABSOLUTELY NO WARRANTY.     This is free
      software  and you are welcome  to redistribute it under certain
      conditions.   For   details   see    attached   COPYING   file.

      transmitest: device node="/dev/pcanusbfd32"
                   Extended frames are sent, init with BTR0BTR1=0x0014
                   Data will be read from "transmit.txt".
      transmitest: driver version = Release_20250213_n
      transmitest: CAN_Init(): Device or resource busy
      transmitest: type           = usbfd
                   Serial Number   = 0x00000000
                   Device Number   = 1
                   count of reads  = 27222
                   count of writes = 0
                   count of errors = 0
                   count of irqs   = 1075
                   last CAN status = 0x0000
                   last error      = 0
                   open paths      = 2
                   driver version  = Release_20250213_n
      transmitest: finished (16).
  ```
