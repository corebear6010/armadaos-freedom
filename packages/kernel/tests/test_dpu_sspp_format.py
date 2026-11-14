#!/usr/bin/env python3
"""Run actual SSPP format writers against a register array."""
import argparse
import pathlib
import re
import subprocess
import sys
from test_dpu_rotation import function

PREFIX = r"""
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <assert.h>
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define test_bit(n,p) (!!(*(p) & BIT(n)))
#define DRM_WARN_ONCE(...) assert(0)
#define MDP_FETCH_LINEAR 0
#define MDP_PLANE_INTERLEAVED 0
#define CHROMA_H2V1 1
#define CHROMA_H1V2 2
#define DPU_SSPP_ROT_90 BIT(0)
#define DPU_SSPP_SOURCE_ROTATED_90 BIT(1)
#define DPU_SSPP_FLIP_LR BIT(2)
#define DPU_SSPP_FLIP_UD BIT(3)
#define DPU_SSPP_SOLID_FILL BIT(4)
#define DPU_SSPP_CSC 0
#define DPU_SSPP_CSC_10BIT 1
#define VIG_OP_CSC_EN 1
#define VIG_OP_CSC_SRC_DATAFMT 2
#define VIG_CSC_10_EN 1
#define VIG_CSC_10_SRC_DATAFMT 2
#define MSM_FORMAT_FLAG_UNPACK_TIGHT BIT(0)
#define MSM_FORMAT_FLAG_UNPACK_ALIGN_MSB BIT(1)
#define MSM_FORMAT_IS_UBWC(f) ((f)->ubwc)
#define MSM_FORMAT_IS_YUV(f) ((f)->yuv)
#define MSM_FORMAT_IS_DX(f) ((f)->dx)
#define UBWC_1_0 1
#define UBWC_2_0 2
#define UBWC_3_0 3
#define UBWC_4_0 4
#define UBWC_5_0 5
#define UBWC_6_0 6
#define UBWC_SWIZZLE_ENABLE_LVL1 1
enum dpu_sspp_multirect_index { DPU_SSPP_RECT_SOLO, DPU_SSPP_RECT_0, DPU_SSPP_RECT_1 };
struct dpu_hw_blk_reg_map { u32 reg[1024]; };
struct block { u32 base; };
struct sblk { struct block sspp_rec0_blk, sspp_rec1_blk; };
struct cap { unsigned long features; const struct sblk *sblk; };
struct ver { unsigned int core_major_ver; };
struct ubwc { unsigned int highest_bank_bit, ubwc_enc_version; };
struct dpu_hw_sspp {
 struct dpu_hw_blk_reg_map hw;
 const struct cap *cap;
 const struct ver *mdss_ver;
 const struct ubwc *ubwc;
};
struct dpu_sw_pipe { struct dpu_hw_sspp *sspp; enum dpu_sspp_multirect_index multirect_index; };
struct msm_format {
 u32 chroma_sample,fetch_type,bpc_a,bpc_r_cr,bpc_b_cb,bpc_g_y;
 u32 alpha_enable,element[4],unpack_count,flags,bpp,fetch_mode;
 bool ubwc,yuv,dx;
};
static u32 reg_read(struct dpu_hw_blk_reg_map *c, u32 off) {
 assert(!(off & 3) && off/4 < 1024); return c->reg[off/4];
}
static void reg_write(struct dpu_hw_blk_reg_map *c, u32 off, u32 value) {
 assert(!(off & 3) && off/4 < 1024); c->reg[off/4]=value;
}
#define DPU_REG_READ(c,o) reg_read(c,o)
#define DPU_REG_WRITE(c,o,v) reg_write(c,o,v)
static u32 qcom_ubwc_swizzle(const struct ubwc *u) { (void)u; return 0; }
static void dpu_hw_sspp_setup_opmode(struct dpu_hw_sspp *c,u32 m,bool e)
{ (void)c; (void)m; (void)e; }
static void dpu_hw_sspp_setup_csc10_opmode(struct dpu_hw_sspp *c,u32 m,bool e)
{ (void)c; (void)m; (void)e; }
"""
TESTS = r"""
static int cases, failures;
#define CHECK(x) do { if (!(x)) { failures++; \
 fprintf(stderr,"case %d line %d: %s\n",cases,__LINE__,#x); } } while(0)

static void run_case(int major, enum dpu_sspp_multirect_index rect,
                     bool rotation, bool stale_rotation, bool dx, bool ubwc,
                     u32 flip, int bpp)
{
 const struct sblk sb={.sspp_rec0_blk={0x200},.sspp_rec1_blk={0x400}};
 const struct cap cap={.sblk=&sb};
 const struct ver ver={major};
 const struct ubwc uc={.highest_bank_bit=16,.ubwc_enc_version=6};
 struct dpu_hw_sspp ctx={.cap=&cap,.mdss_ver=&ver,.ubwc=&uc};
 struct dpu_sw_pipe pipe={&ctx,rect};
 const struct msm_format fmt={
  .bpc_a=3,.bpc_r_cr=3,.bpc_b_cb=3,.bpc_g_y=3,
  .element={2,0,1,3},.unpack_count=4,.bpp=bpp,
  .fetch_mode=ubwc?2:0,.ubwc=ubwc,.dx=dx,
  .flags=MSM_FORMAT_FLAG_UNPACK_TIGHT,
 };
 u32 base=rect==DPU_SSPP_RECT_1?0x400:0x200;
 u32 op=base+SSPP_REC_SRC_OP_MODE, format=base+SSPP_REC_SRC_FORMAT;
 u32 other=rect==DPU_SSPP_RECT_1?0x200:0x400;
 u32 flags=flip | (rotation?DPU_SSPP_ROT_90:0);
 cases++;
 reg_write(&ctx.hw,op,BIT(18)|(stale_rotation?BIT(15):0));
 reg_write(&ctx.hw,other+SSPP_REC_SRC_FORMAT,0x12345678);
 if (major>=13) {
  dpu_hw_sspp_setup_format_v13(&pipe,&fmt,flags);
  CHECK(!!(reg_read(&ctx.hw,op)&BIT(15))==rotation);
  CHECK(((reg_read(&ctx.hw,format)>>9)&7)==(u32)(bpp-1));
 } else {
  dpu_hw_setup_format_impl(&pipe,&fmt,flags,&ctx,op,
    base+SSPP_REC_SRC_UNPACK_PATTERN,format,
    base+SSPP_REC_UBWC_STATIC_CTRL,base+SSPP_REC_UBWC_ERROR_STATUS);
  CHECK(!!(reg_read(&ctx.hw,format)&BIT(11))==rotation);
  CHECK(!!(reg_read(&ctx.hw,op)&BIT(15))==stale_rotation);
  CHECK(((reg_read(&ctx.hw,format)>>9)&3)==(u32)(bpp-1));
 }
 CHECK(!!(reg_read(&ctx.hw,format)&BIT(14))==dx);
 CHECK(!!(reg_read(&ctx.hw,op)&BIT(13))==!!(flip&DPU_SSPP_FLIP_LR));
 CHECK(!!(reg_read(&ctx.hw,op)&BIT(14))==!!(flip&DPU_SSPP_FLIP_UD));
 CHECK(reg_read(&ctx.hw,op)&BIT(18));
 CHECK(!!(reg_read(&ctx.hw,op)&BIT(0))==ubwc);
 CHECK(reg_read(&ctx.hw,base+SSPP_REC_SRC_UNPACK_PATTERN)==0x03010002);
 CHECK(reg_read(&ctx.hw,other+SSPP_REC_SRC_FORMAT)==0x12345678);
}
int main(void)
{
 for (int major=12;major<=13;major++)
  for (int rect=0;rect<3;rect++)
   for (int dx=0;dx<2;dx++) {
    run_case(major,rect,true,false,dx,true,DPU_SSPP_FLIP_LR|DPU_SSPP_FLIP_UD,4);
    run_case(major,rect,false,true,dx,true,0,4);
    run_case(major,rect,false,false,dx,false,DPU_SSPP_FLIP_LR,4);
   }
 run_case(13,DPU_SSPP_RECT_SOLO,false,true,false,false,0,8);
 run_case(13,DPU_SSPP_RECT_SOLO,true,false,false,true,0,8);
 printf("%d register cases, %d failed assertions\n",cases,failures);
 return failures?1:0;
}
"""

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("source",type=pathlib.Path)
    parser.add_argument("--work",type=pathlib.Path,required=True)
    args=parser.parse_args()
    root=args.source/"drivers/gpu/drm/msm/disp/dpu1"
    common=(root/"dpu_hw_sspp.c").read_text()
    v13=(root/"dpu_hw_sspp_v13.c").read_text()
    defines={}
    for text in (common,v13):
        for line in text.splitlines():
            match=re.match(r"#define\s+((?:SSPP_|MDSS_MDP_OP_)\w+)\s+",line)
            if match:
                defines[match[1]]=line
    args.work.mkdir(parents=True,exist_ok=True)
    src=args.work/"sspp_format_fixture.c"
    src.write_text("\n\n".join([
        PREFIX,"\n".join(defines.values()),
        function(common,"dpu_hw_setup_format_impl"),
        function(v13,"dpu_hw_sspp_calculate_rect_off"),
        function(v13,"dpu_hw_sspp_setup_format_v13"),TESTS]))
    exe=args.work/"sspp_format_fixture"
    subprocess.run(["gcc","-std=gnu11","-Wall","-Wextra","-Werror",
                    "-Wno-unused-parameter","-O1","-g","-fsanitize=address,undefined",
                    str(src),"-o",str(exe)],check=True)
    return subprocess.run([str(exe)]).returncode

if __name__=="__main__":
    sys.exit(main())
