/**
 * Creates static struct in bootloader firmware containing build version.
 */

#include <stdint.h>
#include "genhdr/mpversion.h"

typedef struct __attribute__((__packed__)) _fw_footer
{
    uint8_t footer_version;
    uint16_t footer_length;
    char vershdr[4];
    char version[57];
} fw_footer_t;

// This will be located at 0x08000280 in mboot_footer.ld
static const fw_footer_t __attribute__((used, section(".text.fw_footer"))) fw_footer = {
    .footer_version = 1,
    .footer_length = 64,
    .vershdr = "VER:",
    .version = MICROPY_GIT_TAG,
};
