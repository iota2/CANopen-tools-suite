> `iota2` - Making Imaginations, Real
>
> <i2.iotasquare@gmail.com>


```
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
```

# Installing Drivers for Linux

- Check if `CAN` drivers part of your Linux environment
  ```bash
  grep PEAK_ /boot/config-`uname -r`
      CONFIG_CAN_PEAK_PCIEFD=m
      CONFIG_CAN_PEAK_PCI=m
      CONFIG_CAN_PEAK_PCIEC=y
      CONFIG_CAN_PEAK_PCMCIA=m
      CONFIG_CAN_PEAK_USB=m
  ```

- Check if the `CAN` device is initialized
  - Insert `PEAK CAN` into USB port
    ```bash
    lsmod | grep ^peak
        peak_usb               61440  0
    ```

- Check which kernel version is required for your PEAK CAN interface
  - Download the packed script: [pcan-kernel-version.sh.tar.gz](https://www.peak-system.com/fileadmin/media/linux/files/pcan-kernel-version.sh.tar.gz)

  - To extract it open a terminal and type:
    ```bash
    tar -xzf pcan-kernel-version.sh.tar.gz
    ```

  - Execute the script with
    ```bash
    ./pcan-kernel-version.sh
        Bus 003 Device 017: ID 0c72:0012 PEAK System PCAN-USB FD needs Linux 4.0
    ```

- USB - CAN Socket Driver: dealing with `canX` interfaces names under Linux 6.3

Connecting, disconnecting, reconnecting USB - CAN interfaces is convenient since the driver loads itself into memory, but it has the disadvantage that the name of the CAN interfaces depends on the connection order. Using a (so-called) "device id" in this case solves this problem. The device id is a number chosen by the user and flashed in the device itself.  Starting from Linux 6.3, the device id is attached to a CAN channel (unlike Windows where the device id is attached to the device itself). Thus, a `PCAN-USB` Pro FD will offer to define TWO device ids under Linux while only one will be possible under Windows. To access the flash memory of the USB - CAN interface, one uses the `ethtool` tool as follows:

- Install `ethtool`
  ```bash
  sudo apt-get install ethtool
  ```

- To read the flash memory of "can0" interface:
  ```bash
  sudo ethtool -e can0 raw off
      Offset          Values
      ------          ------
      0x0000:         00 00 00 00
  ```

- To write value `1`:
  ```bash
  sudo ethtool -E can0 value 1
  ```

- This device id is also readable through the `sysfs` interface, by displaying the content of the `can_channel_id` file of the concerned CAN network interface:
  ```bash
  cat /sys/class/net/can0/peak_usb/can_channel_id
      00000001
  ```

- The purpose of this number is to be used for the naming of the associated CAN interface. This is made possible by the `Udev` daemon and:
  - Adding a new appropriate rule in (for example) the file "70-persistent-net.rules":
    ```bash
    sudo vim /etc/udev/rules.d/70-persistent-net.rules
        SUBSYSTEM=="net", ACTION=="add", DRIVERS=="peak_usb", KERNEL=="can*", PROGRAM="/bin/peak_usb_device_namer %k", NAME="%c"
    ````

  - Adding the following shell script (for example) "/bin/peak_usb_device_namer":
    ```bash
    sudo vim /bin/peak_usb_device_namer
    ```
    ```bash
    #!/bin/sh
    #
    # External Udev program to rename peak_usb CAN interfaces according to the flashed device numbers.
    #
    # (C) 2023 PEAK-System GmbH by Stephane Grosjean
    #
    [ -z "$1" ] && exit 1
    CAN_ID="/sys/class/net/$1/peak_usb/can_channel_id"
    if [ -f $CAN_ID ]; then
        devid=`cat $CAN_ID`
        # PCAN-USB specific: use "000000FF" instead of "FFFFFFFF" below
        if [ "$devid" != "00000000" -a "$devid" != "FFFFFFFF" ]; then
            printf "can%d\n" 0x${devid}
            exit 0
        fi
    fi
    echo $1
    ```

# Enabling CAN interface

Assuming the device is recognized as `can0`:
```bash
sudo ip link set can0 type can bitrate 1000000
sudo ip link set can0 up
```

Virtual `CAN` setup
```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
```

# Disable CAN interface

Assuming the device is recognized as `can0`:
```bash
sudo ip link set can0 down
```

# CAN-Interface

## Load Drivers

- By convention, the value of the device id when leaving the factory is either `00000000` or `FFFFFFFF` (`000000FF` specifically for `PCAN-USB`). In these specific cases, the above script considers that the CAN interface should not be renamed. Once these changes have been made, the `peak_usb` driver must be unloaded from memory (if it was previously loaded) and then reloaded:
  ```bash
  sudo rmmod peak_usb
  sudo modprobe peak_usb
  ```

- Install `CAN-Utils`
  ```bash
  sudo apt install can-utils
  ```

# Installing PCAN-Driver

- Install Kernel Headers & Build Tools
  ```bash
  sudo apt update
  sudo apt install linux-headers-$(uname -r) build-essential libpopt-dev libelf-dev
  ```

- Make sure the system `gcc` version is `12.x`, or use following:
  ```bash
  gcc –version
  ```

  - If `gcc` version requirement not met, install the required version
    ```bash
    sudo apt update
    sudo apt install gcc-12 g++-12
    gcc-12 --version
    g++-12 --version
    ```

  - Redirect system to `use gcc-12`
    ```bash
    cd /usr/bin
    sudo ln -sf gcc-12 gcc
    sudo ln -sf gcc-ar-12 gcc-ar
    sudo ln -sf gcc-nm-12 gcc-nm
    sudo ln -sf gcc-ranlib-12 gcc-ranlib
    ```

  - Verify `gcc` version again
    ```bash
    gcc --version
    ```
- Download latest [PCAN drivers](https://www.peak-system.com/fileadmin/media/linux/index.php)

- Untar the compressed driver file (Installed 8.20.0):
  ```bash
  tar -xzf peak-linux-driver-X.Y.Z.tar.gz
  cd peak-linux-driver-X.Y.Z
  ```

- Clean the repository
  ```bash
  make clean
  ```

- Build for real time `netdev` interface
  ```bash
  make netdev
  ```

- Install the drivers
  ```bash
  sudo make install
  ```

- Install the modules
  ```bash
  sudo modprobe pcan
  sudo modprobe can
  sudo modprobe vcan
  sudo modprobe slcan
  sudo modprobe peak_usb
  ```

- CAN IP can be enabled now

- Check installation
  ```bash
  ./driver/lspcan --all
      pcanusbfd32     CAN1    -       80MHz   1M      ACTIVE  0.00    7404    0       2
  ```

  ```bash
  tree /dev/pcan-usb_fd/
      /dev/pcan-usb_fd/
      ├── 0
      │   └── can0 -> ../../pcanusbfd32
      └── devid=1 -> ../pcanusbfd32
      1 directory, 2 files
  ```

  ```bash
  ip -a link
      1: lo:  mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000
           link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
      2: enp0s31f6:  mtu 1500 qdisc fq_codel state UP mode DEFAULT group default qlen 1000
           link/ether XX:XX:XX:XX:XX:XX brd ff:ff:ff:ff:ff:ff
      3: wlp146s0:  mtu 1500 qdisc noqueue state DOWN mode DORMANT group default qlen 1000
           link/ether XX:XX:XX:XX:XX:XX brd ff:ff:ff:ff:ff:ff
      12: can0:  mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
           link/can
  ```

  ```bash
  ip -details link show can0
      12: can0:  mtu 16 qdisc pfifo_fast state UP mode DEFAULT group default qlen 10
           link/can  promiscuity 0 minmtu 0 maxmtu 0
           can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0
                 bitrate 1000000 sample-point 0.750
                 tq 12 prop-seg 29 phase-seg1 30 phase-seg2 20 sjw 10
                 pcan: tseg1 1..256 tseg2 1..128 sjw 1..128 brp 1..1024 brp-inc 1
                 pcan: dtseg1 1..32 dtseg2 1..16 dsjw 1..16 dbrp 1..1024 dbrp-inc 1
                 clock 80000000 numtxqueues 1 numrxqueues 1 gso_max_size 65536 gso_max_segs 65535
  ```

# Installing PCAN-View

- Download and install the following file peak-system.list from the PEAK-System website:
  ```bash
  wget -q [http://www.peak-system.com/debian/dists/`lsb_release](http://www.peak-system.com/debian/dists/%60lsb_release) -cs`/peak-system.list -O- | sudo tee /etc/apt/sources.list.d/peak-system.list
  ```
- Download and install the PEAK-System public key for apt-secure, so that the repository is trusted:
  ```bash
  wget -q http://www.peak-system.com/debian/peak-system-public-key.asc -O- | sudo apt-key add -
  ```
- Install `pcanview-ncurses`:
  ```bash
  sudo apt-get update
  sudo apt-get install pcanview-ncurses
  ```

- Launch `PCAN-View` (Assuming connecting on `can0` interface):
  ```bash
  pcanview can0
  ```
