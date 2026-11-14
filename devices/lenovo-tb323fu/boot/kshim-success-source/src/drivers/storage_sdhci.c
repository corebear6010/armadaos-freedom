/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_sdhci.h>
#include <string.h>
/* Original SDHCI/SD protocol implementation, register semantics cross-checked
 * with Linux330517c63aa9b48227b6ab45f01003effac77ae4 drivers/mmc/host/sdhci.h.
 * SD-card commands only, single-blockPIO,1bit400kHz; no DMA or UHS transition. */
enum { BLOCK=4, COUNT=6, ARG=8, MODE=12, CMD=14, RESP=16, FIFO=32,
       PRESENT=36, HOST=40, POWER=41, CLOCK=44, TIMEOUT=46, RESET=47,
       STATUS=48, INT_EN=52, SIGNAL=56, VERSION=254 };
#define SD_ERRORS UINT32_C(0xffff8000)
#define R1_ERRORS UINT32_C(0xfff9a008)
static int rd(struct kshim_sdhci *h,unsigned off,unsigned width,uint32_t *v)
{ return h->ops.read(h->ops.ctx,off,width,v)?KSHIM_STORAGE_IO:0; }
static int wr(struct kshim_sdhci *h,unsigned off,unsigned width,uint32_t v)
{ return h->ops.write(h->ops.ctx,off,width,v)?KSHIM_STORAGE_IO:0; }
static int card(struct kshim_sdhci *h)
{
    int rc=h->ops.card_present(h->ops.ctx);
    return rc==1?0:rc==0?KSHIM_SD_CARD:KSHIM_STORAGE_IO;
}
static int poll(struct kshim_sdhci *h,unsigned off,unsigned width,
                 uint32_t mask,uint32_t want,uint32_t ms)
{
    uint64_t start=h->ops.now_ms(h->ops.ctx);
    for(unsigned i=0;i<200000;++i){
        uint32_t value;int rc=rd(h,off,width,&value);if(rc)return rc;
        if((value&mask)==want)return 0;
        if(h->ops.now_ms(h->ops.ctx)-start>=ms)return KSHIM_SD_TIMEOUT;
        h->ops.delay_us(h->ops.ctx,10);
    }
    return KSHIM_SD_TIMEOUT;
}
static int status_wait(struct kshim_sdhci *h,uint32_t wanted)
{
    uint64_t start=h->ops.now_ms(h->ops.ctx);
    for(unsigned i=0;i<200000;++i){
        uint32_t value;int rc=card(h);if(rc)return rc;
        if((rc=rd(h,STATUS,4,&value)))return rc;
        if(value&SD_ERRORS)return value&0x10000?KSHIM_SD_TIMEOUT:KSHIM_SD_PROTOCOL;
        if(value&wanted)return 0;
        if(h->ops.now_ms(h->ops.ctx)-start>=1000)return KSHIM_SD_TIMEOUT;
        h->ops.delay_us(h->ops.ctx,10);
    }
    return KSHIM_SD_TIMEOUT;
}
static int reset_lines(struct kshim_sdhci *h,uint8_t mask)
{
    int rc=wr(h,RESET,1,mask);if(rc)return rc;
    return poll(h,RESET,1,mask,0,100);
}
/* flags are the SDHCI response/CRC/index/data bits. response[0] is the
 * normalized most-significant word for136-bit replies, as in the SD spec. */
static int command(struct kshim_sdhci *h,uint8_t index,uint32_t argument,
                    uint16_t flags,uint32_t response[4],void *data,bool write)
{
    if(h->failed)return KSHIM_SD_FAILED;
    int rc=card(h);if(rc)return rc;
    uint32_t inhibit=(data||(flags&3)==3)?3:1;
    if((rc=poll(h,PRESENT,4,inhibit,0,100)))return rc;
    if((rc=wr(h,STATUS,4,UINT32_MAX)))return rc;
    if(data){
        if((rc=wr(h,BLOCK,2,512))||(rc=wr(h,COUNT,2,1))||
           (rc=wr(h,MODE,2,write?2:0x12)))return rc;
    }else if((rc=wr(h,MODE,2,0)))return rc;
    if((rc=wr(h,ARG,4,argument))||
       (rc=wr(h,CMD,2,((uint16_t)index<<8)|flags)))return rc;
    if((rc=status_wait(h,1)))return rc;
    if((rc=wr(h,STATUS,4,1)))return rc;
    if(response){
        uint32_t raw[4]={0};
        if((flags&3)==1){
            for(unsigned i=0;i<4;++i)if((rc=rd(h,RESP+4*i,4,&raw[i])))return rc;
            for(unsigned i=0;i<4;++i)
                response[i]=(raw[3-i]<<8)|(i<3?raw[2-i]>>24:0);
        }else if((rc=rd(h,RESP,4,response)))return rc;
    }
    if(data){
        if(!response||(response[0]&R1_ERRORS))return KSHIM_SD_PROTOCOL;
        if((rc=status_wait(h,write?0x10:0x20)))return rc;
        if((rc=wr(h,STATUS,4,write?0x10:0x20)))return rc;
        uint8_t *p=data;
        for(unsigned i=0;i<128;++i){
            uint32_t value;
            if(write){
                value=(uint32_t)p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;
                rc=wr(h,FIFO,4,value);
            }else{
                rc=rd(h,FIFO,4,&value);
                if(!rc)for(unsigned j=0;j<4;++j)p[j]=value>>(8*j);
            }
            if(rc)return rc;
            p+=4;
        }
    }
    if(data||(flags&3)==3){
        if((rc=status_wait(h,2)))return rc;
        if((rc=wr(h,STATUS,4,2)))return rc;
    }
    return 0;
}
static int ready(struct kshim_sdhci *h)
{
    uint64_t start=h->ops.now_ms(h->ops.ctx);
    for(unsigned i=0;i<1000;++i){
        uint32_t r[4]={0};int rc=command(h,13,(uint32_t)h->rca<<16,0x1a,r,NULL,false);
        if(rc)return rc;
        if(r[0]&R1_ERRORS)return KSHIM_SD_PROTOCOL;
        if((r[0]&0x1f00)==0x900)return 0; /* READY_FOR_DATA and TRAN */
        if(h->ops.now_ms(h->ops.ctx)-start>=1000)return KSHIM_SD_TIMEOUT;
        h->ops.delay_us(h->ops.ctx,1000);
    }
    return KSHIM_SD_TIMEOUT;
}
static int rw_blocks(struct kshim_sdhci *h,uint64_t lba,uint32_t count,void *data,bool write)
{
    if(!h||!h->owned||!data||!count)return KSHIM_STORAGE_ARG;
    if(h->failed)return KSHIM_SD_FAILED;
    if(lba>=h->block.block_count||count>h->block.block_count-lba||
       (uint64_t)count*512>SIZE_MAX)return KSHIM_STORAGE_RANGE;
    uint8_t *p=data;uint8_t sector[512]; /* failed reads do not publish partial data */
    for(uint32_t i=0;i<count;++i){
        uint64_t address=h->high_capacity?lba+i:(lba+i)*512;
        if(address>UINT32_MAX)return KSHIM_STORAGE_RANGE;
        uint32_t r[4]={0};
        int rc=command(h,write?24:17,(uint32_t)address,0x3a,r,write?p:sector,write);
        if(!rc&&write)rc=ready(h);
        if(rc){h->failed=1;return rc;}
        if(!write)memcpy(p,sector,512);
        p+=512;
    }
    return 0;
}
static int block_read(void *h,uint64_t lba,uint32_t count,void *p)
{ return rw_blocks(h,lba,count,p,false); }
static int block_write(void *h,uint64_t lba,uint32_t count,const void *p)
{ return rw_blocks(h,lba,count,(void*)p,true); }
static int block_flush(void *ctx)
{
    struct kshim_sdhci *h=ctx;
    if(!h||!h->owned||h->failed)return KSHIM_SD_FAILED;
    int rc=ready(h);if(rc)h->failed=1;
    return rc;
}
int kshim_sdhci_open(struct kshim_sdhci *h,const struct kshim_sdhci_ops *o,
                     uint32_t parent,uint8_t voltage,uint32_t ocr,bool writes)
{
    if(!h||!o||!o->read||!o->write||!o->now_ms||!o->delay_us||!o->card_present||
       !parent||parent>400000000||(voltage!=0x0c&&voltage!=0x0e)||
       !(ocr&0x00ff8000)||(ocr&~UINT32_C(0x00ff8000)))return KSHIM_STORAGE_ARG;
    if(h->owned)return KSHIM_SD_FAILED;
    memset(h,0,sizeof(*h));h->ops=*o;h->parent_clock_hz=parent;h->ocr_mask=ocr;
    int rc=card(h);if(rc)return rc;
    uint32_t present_state,version,r[4]={0};
    if((rc=rd(h,PRESENT,4,&present_state)))return rc;
    if(present_state&3)return KSHIM_SD_FAILED;
    if((rc=rd(h,VERSION,2,&version)))return rc;
    /* SDHCIv3+ uses even divisors2..2046; v1/2 use powers of2. */
    unsigned divider=(parent+799999)/800000;
    if(parent<=400000)divider=0;
    if((version&255)<2){
        unsigned d=1;while(d<divider&&d<128)d*=2;
        if(divider>128)return KSHIM_SD_UNSUPPORTED;
        divider=d;
    }else if(divider>1023)return KSHIM_SD_UNSUPPORTED;
    h->owned=1;
#define DO(expr) do{rc=(expr);if(rc)goto fail;}while(0)
    DO(reset_lines(h,1));
    DO(wr(h,SIGNAL,4,0));DO(wr(h,INT_EN,4,0x03ff8033));
    DO(wr(h,HOST,1,0));DO(wr(h,POWER,1,voltage|1));
    h->ops.delay_us(h->ops.ctx,100000);
    DO(wr(h,CLOCK,2,0));DO(wr(h,TIMEOUT,1,14));
    uint32_t clk=((divider&255)<<8)|((divider&0x300)>>2)|1;
    DO(wr(h,CLOCK,2,clk));DO(poll(h,CLOCK,2,2,2,100));DO(wr(h,CLOCK,2,clk|4));
    h->ops.delay_us(h->ops.ctx,1000); /* >=74 clocks at400kHz */
    DO(command(h,0,0,0,NULL,NULL,false));
    rc=command(h,8,0x1aa,0x1a,r,NULL,false);
    bool v2=rc==0;
    if(rc==KSHIM_SD_TIMEOUT){DO(reset_lines(h,2));}
    else if(rc)goto fail;
    if(v2&&((r[0]&0xfff)!=0x1aa)){rc=KSHIM_SD_UNSUPPORTED;goto fail;}
    unsigned attempt;
    for(attempt=0;attempt<100;++attempt){
        DO(command(h,55,0,0x1a,r,NULL,false));
        if((r[0]&R1_ERRORS)||!(r[0]&0x20)){rc=KSHIM_SD_PROTOCOL;goto fail;}
        DO(command(h,41,ocr|(v2?1u<<30:0),2,r,NULL,false));
        if(r[0]&0x80000000)break;
        h->ops.delay_us(h->ops.ctx,10000);
    }
    if(attempt==100){rc=KSHIM_SD_TIMEOUT;goto fail;}
    if(!(r[0]&ocr)){rc=KSHIM_SD_UNSUPPORTED;goto fail;}
    h->high_capacity=v2&&((r[0]&(1u<<30))!=0);
    DO(command(h,2,0,9,r,NULL,false));
    DO(command(h,3,0,0x1a,r,NULL,false));
    if((r[0]&0xe000)||!(r[0]>>16)){rc=KSHIM_SD_PROTOCOL;goto fail;}
    h->rca=(uint16_t)(r[0]>>16);
    DO(command(h,9,(uint32_t)h->rca<<16,9,r,NULL,false));
    uint64_t blocks;
    if((r[0]>>30)==1&&h->high_capacity){
        uint32_t size=((r[1]&63)<<16)|(r[2]>>16);
        blocks=((uint64_t)size+1)*1024;
    }else if((r[0]>>30)==0&&!h->high_capacity){
        unsigned read_length=(r[1]>>16)&15,mult=(r[2]>>15)&7;
        uint32_t size=((r[1]&1023)<<2)|(r[2]>>30);
        if(read_length<9||read_length>11){rc=KSHIM_SD_UNSUPPORTED;goto fail;}
        blocks=((uint64_t)size+1)<<(mult+2+read_length-9);
    }else{rc=KSHIM_SD_UNSUPPORTED;goto fail;}
    DO(command(h,7,(uint32_t)h->rca<<16,0x1b,r,NULL,false));
    if(r[0]&R1_ERRORS){rc=KSHIM_SD_PROTOCOL;goto fail;}
    if(!h->high_capacity){
        DO(command(h,16,512,0x1a,r,NULL,false));
        if(r[0]&R1_ERRORS){rc=KSHIM_SD_PROTOCOL;goto fail;}
    }
    h->block=(struct kshim_blockdev){h,512,blocks,block_read,
                                    writes?block_write:NULL,writes?block_flush:NULL};
#undef DO
    return 0;
fail:
    h->failed=1;return rc;
}
int kshim_sdhci_release(struct kshim_sdhci *h)
{
    if(!h)return KSHIM_STORAGE_ARG;
    if(!h->owned)return 0;
    /* PIO has no DMA arena to pin. Stop card/controller activity and clocks
     * even after a failed command; board teardown follows this successful stop. */
    int result=wr(h,SIGNAL,4,0),rc;
    rc=wr(h,INT_EN,4,0);if(!result)result=rc;
    rc=reset_lines(h,6);if(!result)result=rc;
    rc=wr(h,CLOCK,2,0);if(!result)result=rc;
    if(!result){h->owned=0;h->failed=0;}
    return result;
}
