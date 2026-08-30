// Guarded because the port includes this header along two paths (mpconfigport.h
// and the flash HAL) and the upstream definition below it is not itself
// guarded, so a second pass would redefine what this file overrides.
#ifndef MICROPY_INCLUDED_USB2CAN_SEEED_ARCH_MIX_MPCONFIGBOARD_H
#define MICROPY_INCLUDED_USB2CAN_SEEED_ARCH_MIX_MPCONFIGBOARD_H

// Everything about the Arch Mix itself - clocks, flash, pin mux tables and
// both FlexCAN instances - comes from the upstream board definition; this
// file carries only what makes the board a USB2CAN adapter.
#include "ports/mimxrt/boards/SEEED_ARCH_MIX/mpconfigboard.h"

#undef MICROPY_HW_BOARD_NAME
#define MICROPY_HW_BOARD_NAME               "USB2CAN-SEEED_ARCH_MIX"

// CAN2 (FLEXCAN2, J3_14/J3_15) is the instance wired to a transceiver on
// this board; CAN1 has no transceiver and is left unconnected.
#define MICROPY_HW_CAN2_NAME                "CAN2"

#define MICROPY_HW_USB_MANUFACTURER_STRING      "alelec"
#define MICROPY_HW_USB_PRODUCT_FS_STRING        "USB2CAN"
#define MICROPY_HW_USB_PRODUCT_HS_STRING        "USB2CAN"

#endif // MICROPY_INCLUDED_USB2CAN_SEEED_ARCH_MIX_MPCONFIGBOARD_H
