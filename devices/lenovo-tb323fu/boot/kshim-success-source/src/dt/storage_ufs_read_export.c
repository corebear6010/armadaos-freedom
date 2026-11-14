/* SPDX-License-Identifier: GPL-3.0-only */
#include "storage_ufs_read_export.h"
#include <libfdt.h>

static void put_be32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)(value >> 24);
    p[1] = (uint8_t)(value >> 16);
    p[2] = (uint8_t)(value >> 8);
    p[3] = (uint8_t)value;
}
static void put_be64(uint8_t *p, uint64_t value)
{
    put_be32(p, (uint32_t)(value >> 32));
    put_be32(p + 4, (uint32_t)value);
}
int storage_ufs_read_export(void *fdt,
                            const struct kshim_ufs_read_report *report)
{
    if (!fdt || !report || report->version != KSHIM_UFS_READ_EXPORT_VERSION)
        return -FDT_ERR_BADVALUE;
    int rc = fdt_check_header(fdt);
    if (rc) return rc;
    int node = fdt_path_offset(fdt, "/chosen");
    if (node < 0) return node;

    uint8_t payload[KSHIM_UFS_READ_EXPORT_BYTES];
    put_be32(payload + 0, report->version);
    put_be32(payload + 4, report->board);
    put_be32(payload + 8, report->stage);
    put_be32(payload + 12, (uint32_t)report->status);
    put_be32(payload + 16, (uint32_t)report->release_status);
    put_be32(payload + 20, report->restore_verified);
    put_be32(payload + 24, report->handoff_safe);
    put_be32(payload + 28, report->block_size);
    put_be64(payload + 32, report->block_count);
    put_be64(payload + 40, report->dma_base);
    put_be64(payload + 48, report->first_lba);
    put_be64(payload + 56, report->last_lba);
    for (unsigned i = 0; i < sizeof(report->partition_uuid); ++i)
        payload[64 + i] = report->partition_uuid[i];
    put_be32(payload + 80, report->header_crc);
    put_be32(payload + 84, report->tail_crc);
    for (unsigned i = 0; i < sizeof(report->header_prefix); ++i)
        payload[88 + i] = report->header_prefix[i];
    put_be32(payload + 120, report->hcs);
    put_be32(payload + 124, report->is);
    put_be32(payload + 128, report->doorbell);
    put_be32(payload + 132, report->last_scsi_status);
    put_be32(payload + 136, report->last_sense_key);
    put_be32(payload + 140, report->ocs);
    for (unsigned i = 0; i < sizeof(report->response_prefix); ++i)
        payload[144 + i] = report->response_prefix[i];
    put_be32(payload + 160, report->debug_valid);
    /* One libfdt insertion publishes the whole property; no header-only or
     * truncated record can be observed, and NOSPACE keeps an older one. */
    return fdt_setprop(fdt, node, KSHIM_UFS_READ_FDT_PROPERTY, payload,
                       (int)sizeof(payload));
}
/* Nine u32 per state: the seven uint32 register/FSM fields followed by the two
 * uint8 flags, widened so the record has no padding and no implicit gap. */
static void put_link_state(uint8_t *p, const struct kshim_ufs_link_state *link)
{
    put_be32(p + 0, link->initial_tx);
    put_be32(p + 4, link->current_tx);
    put_be32(p + 8, link->last_command);
    put_be32(p + 12, link->command_result);
    put_be32(p + 16, link->last_is);
    put_be32(p + 20, link->power_status);
    put_be32(p + 24, link->phase);
    put_be32(p + 28, (uint32_t)link->uncertain);
    put_be32(p + 32, (uint32_t)link->restore_hibern8);
}
int storage_ufs_link_export(void *fdt,
                            const struct kshim_ufs_read_report *report)
{
    /* Same gate as the read exporter, and the same version field: a foreign or
     * stale report must not publish link evidence either. */
    if (!fdt || !report || report->version != KSHIM_UFS_READ_EXPORT_VERSION)
        return -FDT_ERR_BADVALUE;
    int rc = fdt_check_header(fdt);
    if (rc) return rc;
    int node = fdt_path_offset(fdt, "/chosen");
    if (node < 0) return node;

    uint8_t payload[KSHIM_UFS_LINK_EXPORT_BYTES];
    put_be32(payload + 0, KSHIM_UFS_LINK_EXPORT_VERSION);
    put_link_state(payload + 4, &report->link_before_release);
    put_link_state(payload + 40, &report->link_after_release);
    /* Separate property, so the frozen 164-byte read record is never rewritten
     * by this exporter; one insertion or none at all. */
    return fdt_setprop(fdt, node, KSHIM_UFS_LINK_FDT_PROPERTY, payload,
                       (int)sizeof(payload));
}

static void put_power_state(uint8_t *p,const struct kshim_ufs_power_state *v)
{
    put_be32(p,v->initial_mode);put_be32(p+4,v->current_mode);
    put_be32(p+8,v->phase);put_be32(p+12,v->uncertain);
    put_be32(p+16,v->restore_mode);
}
static void put_scsi_state(uint8_t *p,const struct kshim_ufs_scsi_state *v)
{
    put_be32(p,v->opcode);put_be32(p+4,v->lun);put_be32(p+8,v->status);
    put_be32(p+12,v->sense_key);put_be32(p+16,v->asc);
    put_be32(p+20,v->ascq);put_be32(p+24,v->sense_valid);
}
int storage_ufs_power_export(void *fdt,const struct kshim_ufs_read_report *r)
{
    if(!fdt||!r||r->version!=KSHIM_UFS_READ_EXPORT_VERSION)
        return -FDT_ERR_BADVALUE;
    int rc=fdt_check_header(fdt);if(rc)return rc;
    int node=fdt_path_offset(fdt,"/chosen");if(node<0)return node;
    uint8_t payload[KSHIM_UFS_POWER_EXPORT_BYTES];
    put_be32(payload,KSHIM_UFS_POWER_EXPORT_VERSION);
    put_power_state(payload+4,&r->power_before_release);
    put_power_state(payload+24,&r->power_after_release);
    put_scsi_state(payload+44,&r->scsi_before_release);
    put_scsi_state(payload+72,&r->scsi_after_release);
    return fdt_setprop(fdt,node,KSHIM_UFS_POWER_FDT_PROPERTY,payload,sizeof(payload));
}
