/* SPDX-License-Identifier: GPL-3.0-only */
#include <storage_sdhci.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
struct fixture {
    uint8_t regs[256],disk[512];uint64_t tick;
    unsigned commands,words,cmd17,cmd24;
    int present,timeout,error,read_data;
};
static uint32_t get(struct fixture *f,unsigned off,unsigned n)
{uint32_t v=0;for(unsigned i=0;i<n;++i)v|=(uint32_t)f->regs[off+i]<<(8*i);return v;}
static void put(struct fixture *f,unsigned off,unsigned n,uint32_t v)
{for(unsigned i=0;i<n;++i)f->regs[off+i]=(uint8_t)(v>>(8*i));}
static int rd(void *p,uint32_t off,unsigned n,uint32_t *v){
    struct fixture *f=p;assert(off+n<=256);
    if(off==0x20){
        assert(n==4&&f->read_data&&f->words<128);
        unsigned b=4*f->words++;*v=(uint32_t)f->disk[b]|(uint32_t)f->disk[b+1]<<8|
            (uint32_t)f->disk[b+2]<<16|(uint32_t)f->disk[b+3]<<24;
        if(f->words==128)put(f,0x30,4,2);
    }else *v=get(f,off,n);
    return 0;
}
static int wr(void *p,uint32_t off,unsigned n,uint32_t v){
    struct fixture *f=p;assert(off+n<=256);
    if(off==0x30){put(f,off,n,get(f,off,n)&~v);return 0;}
    if(off==0x2f){put(f,off,n,0);return 0;}
    if(off==0x2c){put(f,off,n,v|(v&1?2:0));return 0;}
    if(off==0x20){
        assert(n==4&&!f->read_data&&f->words<128);
        unsigned b=4*f->words++;for(unsigned i=0;i<4;++i)f->disk[b+i]=v>>(8*i);
        if(f->words==128)put(f,0x30,4,2);
        return 0;
    }
    put(f,off,n,v);
    if(off!=0x0e)return 0;
    assert(n==2);unsigned cmd=v>>8;++f->commands;
    if(f->timeout)return 0;
    put(f,0x30,4,f->error?0x208000:1);
    if(f->error)return 0;
    put(f,0x10,4,0x900);
    switch(cmd){
    case 0:break;
    case 8:assert(get(f,8,4)==0x1aa);put(f,0x10,4,0x1aa);break;
    case 55:put(f,0x10,4,0x920);break;
    case 41:put(f,0x10,4,0xc0ff8000);break;
    case 2:break;
    case 3:put(f,0x10,4,0x12340000);break;
    case 9: /* SDHCI stripsCRC; normalizedCSD v2,C_SIZE7 =>8192 sectors. */
        put(f,0x1c,4,0x00400000);put(f,0x18,4,0);
        put(f,0x14,4,0x00000700);put(f,0x10,4,0);break;
    case 7:put(f,0x30,4,3);break;
    case 13:break;
    case 17:
    case 24:
        assert(get(f,4,2)==512&&get(f,6,2)==1);
        assert((get(f,0x0c,2)&1)==0); /* PIO only */
        assert((get(f,0x28,1)&0x26)==0); /* 1bit, low speed */
        assert(get(f,8,4)==7); /* Sector address, not byte address. */
        f->words=0;f->read_data=cmd==17;
        if(cmd==17){++f->cmd17;put(f,0x30,4,0x21);}
        else {++f->cmd24;put(f,0x30,4,0x11);}
        break;
    default:assert(0);
    }
    return 0;
}
static uint64_t now(void *p){return ((struct fixture*)p)->tick;}
static void delay(void *p,uint32_t u){((struct fixture*)p)->tick+=(u+999)/1000;}
static int present(void *p){return ((struct fixture*)p)->present;}
static struct kshim_sdhci_ops ops(struct fixture *f)
{return (struct kshim_sdhci_ops){f,rd,wr,now,delay,present};}
static void init(struct fixture *f){memset(f,0,sizeof(*f));f->present=1;put(f,0xfe,2,3);}
int main(void){
    struct fixture f;struct kshim_sdhci h={0};uint8_t data[512];
    init(&f);struct kshim_sdhci_ops o=ops(&f);
    assert(kshim_sdhci_open(&h,&o,200000000,0x0c,0x60000,true)==0);
    assert(h.block.block_size==512&&h.block.block_count==8192&&h.rca==0x1234);
    memset(f.disk,0xa6,512);assert(kshim_block_read(&h.block,7,1,data)==0);
    assert(f.cmd17==1&&data[511]==0xa6);
    memset(data,0x6a,512);assert(kshim_block_write(&h.block,7,1,data)==0);
    assert(f.cmd24==1&&f.disk[511]==0x6a);
    assert(kshim_block_flush(&h.block)==0);
    unsigned before=f.commands;
    assert(kshim_block_read(&h.block,8192,1,data)==KSHIM_STORAGE_RANGE&&f.commands==before);
    f.present=0;assert(kshim_block_read(&h.block,7,1,data)!=0&&f.commands==before);
    assert(kshim_sdhci_release(&h)==0&&!h.owned);
    assert(get(&f,0x2c,2)==0&&get(&f,0x38,4)==0);
    init(&f);memset(&h,0,sizeof(h));o=ops(&f);
    assert(kshim_sdhci_open(&h,&o,200000000,0x0c,0x60000,false)==0);
    assert(kshim_block_write(&h.block,7,1,data)==KSHIM_STORAGE_READ_ONLY);
    f.error=1;assert(kshim_block_read(&h.block,7,1,data)!=0);
    before=f.commands;assert(kshim_block_read(&h.block,7,1,data)!=0&&f.commands==before);
    assert(kshim_sdhci_release(&h)==0);
    init(&f);memset(&h,0,sizeof(h));o=ops(&f);f.timeout=1;
    assert(kshim_sdhci_open(&h,&o,200000000,0x0c,0x60000,false)==KSHIM_SD_TIMEOUT);
    assert(f.tick<=1200);assert(kshim_sdhci_release(&h)==0);
    init(&f);memset(&h,0,sizeof(h));o=ops(&f);f.present=0;
    assert(kshim_sdhci_open(&h,&o,200000000,0x0c,0x60000,false)==KSHIM_SD_CARD&&f.commands==0);
    puts("SDHCI PIO initialization/read/write/flush/removal/timeout/handoff: PASS");
}
