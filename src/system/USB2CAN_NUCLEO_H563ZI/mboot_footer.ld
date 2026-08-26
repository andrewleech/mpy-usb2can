/**
 * To be passed to linker in MBOOT_LD_FILES to place the footer structure.
 */

SECTIONS
{
    .fw_footer 0x08000280 : /* Past the end of the H563 isr_vector at 0x0800024b */
    {
        KEEP(*(.text.fw_footer))
        . = ALIGN(4);
    } >FLASH_BL

}
INSERT BEFORE .text;
