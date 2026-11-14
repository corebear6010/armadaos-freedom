/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_settings_export.h>
#include <libfdt.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
static unsigned char tree[4096];
static uint32_t u32(const uint8_t *p){return (uint32_t)p[0]<<24|(uint32_t)p[1]<<16|(uint32_t)p[2]<<8|p[3];}
int main(void){
    static struct kshim_ufs_settings b;
    assert(fdt_create_empty_tree(tree,sizeof(tree))==0);
    assert(storage_settings_export(tree,&b,0,0)==-FDT_ERR_NOTFOUND);
    assert(fdt_add_subnode(tree,0,"chosen")>=0);
    b.board=1;b.load_status=-8;b.begin_status=-22;b.release_status=0;
    b.settings.source=KSHIM_SETTINGS_INTERNAL;b.settings.sequence=0x1122334455667788ULL;
    b.settings.values=(struct kshim_config_values){0,2,1, 0, 0};b.save_attempts=3;b.settings.values.last_target=2;b.settings.values.next_action=1;
    assert(storage_settings_export(tree,&b,1,-42)==0);
    int node=fdt_path_offset(tree,"/chosen"),length=0;
    const uint8_t *p=fdt_getprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY,&length);
    assert(p&&length==64&&u32(p)==1&&u32(p+4)==1);
    assert((int32_t)u32(p+8)==-8&&(int32_t)u32(p+12)==-22&&u32(p+16)==0&&u32(p+20)==1);
    assert(u32(p+24)==0x11223344&&u32(p+28)==0x55667788);
    assert(u32(p+32)==0&&u32(p+36)==2&&u32(p+40)==1&&u32(p+44)==3);
    assert(u32(p+48)==0&&u32(p+52)==1&&u32(p+56)==1&&(int32_t)u32(p+60)==-42);
    const uint8_t *history=fdt_getprop(tree,node,"kshim,last-boot-v1",&length);
    assert(history && length==8 && u32(history)==1 && u32(history+4)==2);
    const uint8_t *action=fdt_getprop(tree,node,"kshim,next-action",&length);
    assert(action && length==4 && u32(action)==1);
    b.unsafe=1;b.settings.save_uncertain=1;
    assert(storage_settings_export(tree,&b,0,0)==0);
    p=fdt_getprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY,&length);
    assert(u32(p+48)==1&&u32(p+52)==0&&u32(p+56)==0);
    b.unsafe=0;b.host.owned=1;assert(storage_settings_export(tree,&b,0,0)==0);
    p=fdt_getprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY,&length);assert(u32(p+52)==0);
    uint8_t before[64];memcpy(before,p,64);
    assert(storage_settings_export(tree,NULL,0,0)==-FDT_ERR_BADVALUE);
    p=fdt_getprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY,&length);assert(memcmp(before,p,64)==0);
    assert(storage_settings_export(NULL,&b,0,0)==-FDT_ERR_BADVALUE);
    assert(fdt_delprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY)==0);
    assert(fdt_pack(tree)==0);
    assert(storage_settings_export(tree,&b,0,0)==-FDT_ERR_NOSPACE);
    assert(!fdt_getprop(tree,node,KSHIM_SETTINGS_FDT_PROPERTY,&length));
    puts("Settings FDT layout/endian/ownership/error atomicity: PASS");
}
