/* SPDX-License-Identifier: MIT */
#pragma once
#include <stdint.h>

enum kshim_qcom_reboot_target {
    KSHIM_QCOM_REBOOT_RECOVERY,
    KSHIM_QCOM_REBOOT_BOOTLOADER,
    KSHIM_QCOM_REBOOT_EDL,
};
enum kshim_qcom_reboot_error {
    KSHIM_QCOM_REBOOT_UNSUPPORTED = -100,
    KSHIM_QCOM_REBOOT_INVALID_DT = -101,
    KSHIM_QCOM_REBOOT_IO_FAILED = -102,
    KSHIM_QCOM_REBOOT_VERIFY_FAILED = -103,
    KSHIM_QCOM_REBOOT_RESET_RETURNED = -104,
    KSHIM_QCOM_REBOOT_ROLLBACK_FAILED = -105,
};

struct kshim_qcom_reboot_config {
    const void *fdt;
    uint64_t dload_address;
    uint16_t reason_address;
    uint8_t controller, bus, sid;
    uint8_t reason_shift, reason_mask;
};

/* Caller supplies the already validated, immutable hardware DT used to
 * initialize resources. Only an explicit menu confirmation may call request.
 * Successful reset never returns; every returned value is a failure. */
int kshim_qcom_reboot_parse(const void *hardware_fdt,
                          struct kshim_qcom_reboot_config *config);
int kshim_qcom_reboot_request(const void *hardware_fdt,
                            enum kshim_qcom_reboot_target target);

/* Read-only diagnostic of the existing EDL selection rules. This never writes
 * a download cookie, touches SPMI or invokes either reset operation. */
enum kshim_qcom_edl_route {
    KSHIM_QCOM_EDL_ROUTE_NONE,
    KSHIM_QCOM_EDL_ROUTE_OEM,
    KSHIM_QCOM_EDL_ROUTE_PSCI_RESET2,
};
struct kshim_qcom_edl_probe {
    int32_t status;
    uint32_t scm_available_mask, psci_version;
    int32_t reset2_status, oem_status;
    uint32_t oem_available;
    uint64_t dload_address;
    uint32_t dload_value, route;
};
int kshim_qcom_reboot_probe_edl(const void *hardware_fdt,
                               struct kshim_qcom_edl_probe *report);

/* Weak hardware boundaries allow host tests to observe exact transactions.
 * The default SPMI adapter permits only the DT-selected SDAM byte. */
void kshim_qcom_reboot_smccc(uint64_t registers[8]);
int kshim_qcom_reboot_spmi_prepare(const struct kshim_qcom_reboot_config *config);
int kshim_qcom_reboot_spmi_read(const struct kshim_qcom_reboot_config *config,
                              uint8_t *value);
int kshim_qcom_reboot_spmi_write(const struct kshim_qcom_reboot_config *config,
                               uint8_t value);
