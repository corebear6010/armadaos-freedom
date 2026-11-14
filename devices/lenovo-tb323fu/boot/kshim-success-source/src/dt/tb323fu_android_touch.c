/* SPDX-License-Identifier: MIT */
#include <dt.h>
#include "tb323fu_android_touch.h"

int tb323fu_android_touch_spi_trial(void *fdt, size_t available)
{
    static const char parent_path[] =
        "/soc/qcom,qupv3_4_geni_se@1ac0000/spi@1a80000";
    int rc = dt_validate(fdt, available);
    if (rc)
        return rc;
    int parent = fdt_path_offset(fdt, parent_path);
    if (parent < 0)
        return parent;
    int child = fdt_subnode_offset(fdt, parent, "novatek@0");
    if (child < 0)
        return child;
    uint32_t chip_select, frequency;
    if (fdt_node_check_compatible(fdt, child, "novatek,NVT-ts-spi") ||
        !dt_available(fdt, child) ||
        dt_u32(fdt, child, "reg", &chip_select) || chip_select != 0 ||
        dt_u32(fdt, parent, "spi-max-frequency", &frequency))
        return -FDT_ERR_BADVALUE;
    if (frequency == 4800000)
        return 0;
    if (frequency != 19200000)
        return -FDT_ERR_BADVALUE;
    /* Stock spi-msm-geni reads this controller property. Do not add a child
     * property, replace the tree, or change its size/offsets/dynamic data. */
    rc = fdt_setprop_inplace_u32(fdt, parent, "spi-max-frequency", 4800000);
    return rc ? rc : 1;
}
