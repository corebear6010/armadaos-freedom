/* SPDX-License-Identifier: MIT */
/*
 * tests/tb710fu_splash_test.c
 *
 * Host regression tests for TB710FU / SM8650 ("qcom,pineapple") continuous
 * splash detection.
 *
 * The register values in mock_program() are the ones read on the unit at the
 * kShimC menu handoff (CTL0, LM0-3, VIG0-3): two LM pairs (LM1/LM3 are the
 * right mixers), each source-splitting two 800x2000 VIG pipes, VIG source x
 * 0/800/1600/2400 and destination x 0/800/0/800 inside the pair, one
 * 3200x2000 ARGB8888 buffer at 0xd5100000.  The DT offsets are those of the
 * unit's Pineapple v2 SDE node.  Everything else is rejected, including the
 * shapes that only an ID set would have accepted.
 */
#include "qcom_dt.h"

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MDP_BASE   0x0ae00000u
#define MDP_SIZE   0x00084000u
#define MDP_WORDS  (MDP_SIZE / 4u)

#define FB_BASE    0xd5100000u
#define FB_SIZE    0x02b00000u

#define BLOB_SIZE  65536

static const uint32_t CTL_OFF[6] = {
	0x16000u, 0x17000u, 0x18000u, 0x19000u, 0x1a000u, 0x1b000u
};
static const uint32_t SSPP_OFF[10] = {
	0x5000u, 0x7000u, 0x9000u, 0xb000u, 0x25000u,
	0x27000u, 0x29000u, 0x2b000u, 0x2d000u, 0x2f000u
};
/* qcom,sde-mixer-off: six real LMs then four dummy (0xf0f) slots. */
static const uint32_t MIXER_RAW[10] = {
	0x45000u, 0x46000u, 0x47000u, 0x48000u, 0x49000u, 0x4a000u,
	0xf0fu, 0xf0fu, 0xf0fu, 0xf0fu
};

#define CTL0       CTL_OFF[0]
#define LM(m)      MIXER_RAW[m]
#define VIG(n)     SSPP_OFF[n]

/* CTL_LAYER values measured on the unit: VIG0+VIG1 and VIG2+VIG3 at stage 5
 * (bit 24 is the border enable). */
#define LAYER_VIG01  0x0100002du
#define LAYER_VIG23  0x15000140u

/* ---- MMIO model -------------------------------------------------------- */

struct mock {
	uint32_t regs[MDP_WORDS];
	unsigned reads, writes;
	bool no_reads;		/* any read is a failure */
	uintptr_t unstable_off;	/* value flips on the 2nd read of this offset */
	unsigned unstable_reads;
	uint32_t flip;
};

static struct mock g_mock;

static void fail(const char *what, uintptr_t off)
{
	fprintf(stderr, "tb710fu_splash_test: %s (0x%lx)\n", what,
		(unsigned long)off);
	abort();
}

static uint32_t read32(void *context, uintptr_t address)
{
	struct mock *m = context;
	uintptr_t off;

	if (m->no_reads)
		fail("MMIO read before the window was validated", address);
	if (address < MDP_BASE)
		fail("read below MDP base", address);
	off = address - MDP_BASE;
	if ((off & 3u) != 0u)
		fail("unaligned register read", off);
	if (off + 4u > MDP_SIZE)
		fail("register read past MDP size", off);
	m->reads++;
	uint32_t value = m->regs[off / 4u];
	if (m->unstable_off == off && ++m->unstable_reads == 2u)
		value ^= m->flip;
	return value;
}

static void write32(void *context, uintptr_t address, uint32_t value)
{
	struct mock *m = context;

	(void)value;
	m->writes++;
	fail("unexpected MMIO write", address);
}

static void reg(uint32_t off, uint32_t value)
{
	g_mock.regs[off / 4u] = value;
}

static void mock_reset(void)
{
	memset(&g_mock, 0, sizeof(g_mock));
}

static void pipe_program(unsigned n, uint32_t src_x, uint32_t dst_x)
{
	const uint32_t size = (2000u << 16) | 800u;

	reg(VIG(n) + 0x00u, size);		/* src size */
	reg(VIG(n) + 0x08u, src_x);		/* src xy */
	reg(VIG(n) + 0x0cu, size);		/* out size */
	reg(VIG(n) + 0x10u, dst_x);		/* out xy */
	reg(VIG(n) + 0x14u, FB_BASE);		/* addr */
	reg(VIG(n) + 0x24u, 12800u);		/* stride */
	reg(VIG(n) + 0x30u, 0x237ffu);		/* format */
	reg(VIG(n) + 0x34u, 0x03020001u);	/* unpack */
	reg(VIG(n) + 0x38u, 0x80000000u);	/* op */
	reg(VIG(n) + 0x170u, 0u);		/* multirect */
}

/* The measured TB710FU scanout. */
static void mock_program(void)
{
	const uint32_t lm_out = (2000u << 16) | 800u;

	reg(CTL0 + 0xf4u, 6u);
	reg(CTL0 + 0x14u, 0xf0000000u);
	reg(CTL_OFF[1] + 0x14u, 0xf0000000u);	/* inactive CTL1: low nibble 0 */
	reg(CTL0 + 0x00u, LAYER_VIG01);
	reg(CTL0 + 0x04u, LAYER_VIG01);
	reg(CTL0 + 0x08u, LAYER_VIG23);
	reg(CTL0 + 0x0cu, LAYER_VIG23);
	for (unsigned m = 0; m < 4u; ++m) {
		reg(LM(m) + 0x00u, (m & 1u) ? 0x80000000u : 0u);
		reg(LM(m) + 0x04u, lm_out);
	}
	pipe_program(0, 0u, 0u);
	pipe_program(1, 800u, 800u);
	pipe_program(2, 1600u, 0u);
	pipe_program(3, 2400u, 800u);
}

static void io_bind(struct CrIo *io)
{
	memset(io, 0, sizeof(*io));
	io->context = &g_mock;
	io->read32 = read32;
	io->write32 = write32;
}

/* ---- DT synthesis ------------------------------------------------------ */

static unsigned char g_blob[BLOB_SIZE] __attribute__((aligned(8)));

static void set_cells(void *b, int node, const char *name,
		      const uint32_t *values, unsigned count)
{
	fdt32_t tmp[16];

	assert(count <= 16u);
	for (unsigned i = 0; i < count; ++i)
		tmp[i] = cpu_to_fdt32(values[i]);
	assert(fdt_setprop(b, node, name, tmp,
			   (int)(count * sizeof(fdt32_t))) == 0);
}

/* The unit's SDE resources under an exact qcom,pineapple root; mdp_base
 * lets a test model a malformed DT with a zero MDP base. */
static void build_pineapple(uint64_t mdp_base, uint64_t mdp_size)
{
	void *b = g_blob;

	memset(b, 0, BLOB_SIZE);
	assert(fdt_create_empty_tree(b, BLOB_SIZE) == 0);
	assert(fdt_setprop_string(b, 0, "compatible", "qcom,pineapple") == 0);
	assert(fdt_setprop_u32(b, 0, "#address-cells", 2) == 0);
	assert(fdt_setprop_u32(b, 0, "#size-cells", 2) == 0);

	int rm = fdt_add_subnode(b, 0, "reserved-memory");
	assert(rm >= 0);
	assert(fdt_setprop_u32(b, rm, "#address-cells", 2) == 0);
	assert(fdt_setprop_u32(b, rm, "#size-cells", 2) == 0);
	assert(fdt_setprop(b, rm, "ranges", NULL, 0) == 0);
	int cs = fdt_add_subnode(b, rm, "cont_splash_region@d5100000");
	assert(cs >= 0);
	assert(fdt_setprop_string(b, cs, "label", "cont_splash_region") == 0);
	fdt64_t cs_reg[2] = { cpu_to_fdt64(FB_BASE), cpu_to_fdt64(FB_SIZE) };
	assert(fdt_setprop(b, cs, "reg", cs_reg, sizeof(cs_reg)) == 0);

	int sde = fdt_add_subnode(b, 0, "display-subsystem");
	assert(sde >= 0);
	assert(fdt_setprop_string(b, sde, "compatible", "qcom,sde-kms") == 0);
	assert(fdt_setprop_string(b, sde, "reg-names", "mdp_phys") == 0);
	fdt64_t mdp_reg[2] = { cpu_to_fdt64(mdp_base), cpu_to_fdt64(mdp_size) };
	assert(fdt_setprop(b, sde, "reg", mdp_reg, sizeof(mdp_reg)) == 0);
	set_cells(b, sde, "qcom,sde-ctl-off", CTL_OFF, 6u);
	set_cells(b, sde, "qcom,sde-sspp-off", SSPP_OFF, 10u);
	char types[40];
	for (unsigned i = 0; i < 4u; ++i)
		memcpy(types + i * 4u, "vig", 4u);
	for (unsigned i = 0; i < 6u; ++i)
		memcpy(types + 16u + i * 4u, "dma", 4u);
	assert(fdt_setprop(b, sde, "qcom,sde-sspp-type", types,
			   (int)sizeof(types)) == 0);
	set_cells(b, sde, "qcom,sde-mixer-off", MIXER_RAW, 10u);
}

/* ---- expectation helpers ---------------------------------------------- */

static void expect_zero_outputs(const kshim_framebuffer_config_t *fb,
				const kshim_mmu_region_t *mmio)
{
	assert(fb->render_address == 0u && fb->width == 0u && fb->height == 0u &&
	       fb->stride == 0u && fb->bpp == 0u && fb->buffer_size == 0u &&
	       fb->format == 0);
	assert(mmio->base == 0u && mmio->size == 0u && mmio->type == 0 &&
	       mmio->permissions == 0u);
}

static int run(const struct CrIo *io, kshim_framebuffer_config_t *fb,
	       kshim_mmu_region_t *mmio)
{
	unsigned char before[BLOB_SIZE];

	memcpy(before, g_blob, BLOB_SIZE);
	memset(fb, 0x5a, sizeof(*fb));
	memset(mmio, 0x5a, sizeof(*mmio));
	int rc = kshim_dt_splash(g_blob, io, fb, mmio);
	assert(g_mock.writes == 0u);
	assert(memcmp(before, g_blob, BLOB_SIZE) == 0);
	return rc;
}

static void expect_success(const struct CrIo *io)
{
	kshim_framebuffer_config_t fb;
	kshim_mmu_region_t mmio;
	int rc = run(io, &fb, &mmio);

	if (rc != 0)
		fprintf(stderr, "TB710FU splash expected success, got %d\n", rc);
	assert(rc == 0);
	assert(fb.width == 3200u);
	assert(fb.height == 2000u);
	assert(fb.stride == 12800u);
	assert(fb.bpp == 32u);
	assert(fb.format == KSHIM_FB_FORMAT_ARGB8888);
	assert(fb.render_address == FB_BASE);
	assert(fb.buffer_size == 12800u * 2000u);
	assert(mmio.base == MDP_BASE && mmio.size == MDP_SIZE);
	assert(mmio.type == KSHIM_MMU_DEVICE && mmio.permissions == KSHIM_MMU_READ);
}

static void expect_rejected(const struct CrIo *io, const char *what)
{
	kshim_framebuffer_config_t fb;
	kshim_mmu_region_t mmio;
	int rc = run(io, &fb, &mmio);

	if (rc == 0)
		fprintf(stderr, "TB710FU splash: %s was accepted\n", what);
	assert(rc != 0);
	expect_zero_outputs(&fb, &mmio);
}

/* ---- cases ------------------------------------------------------------- */

int main(void)
{
	struct CrIo io;

	io_bind(&io);
	build_pineapple(MDP_BASE, MDP_SIZE);

	/* 1. The measured layout: two LM pairs, four 800-wide VIG pipes. */
	mock_reset();
	mock_program();
	expect_success(&io);

	/* 2. Reviewer's case: VIG0 (x 0) and VIG1 (x 800) source-split on LM0
	 *    alone, which is only 800 wide; the right half would be cropped.
	 *    The staged ID set {VIG0, VIG1} is the same as on the pair. */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x04u, 0u);
	reg(CTL0 + 0x08u, 0u);
	reg(CTL0 + 0x0cu, 0u);
	expect_rejected(&io, "two pipes on one 800-wide LM");

	/* 3. Same pipe ID set, routing swapped between the pairs: the right
	 *    half of the screen would show the left half of the buffer. */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x00u, LAYER_VIG23);
	reg(CTL0 + 0x04u, LAYER_VIG23);
	reg(CTL0 + 0x08u, LAYER_VIG01);
	reg(CTL0 + 0x0cu, LAYER_VIG01);
	expect_rejected(&io, "pairs routed in swapped order");

	/* 4. Overlap inside a pair: VIG1 placed at destination x 400. */
	mock_reset();
	mock_program();
	reg(VIG(1) + 0x10u, 400u);
	expect_rejected(&io, "overlapping destination rects");

	/* 5. Pipes of a pair at different blend stages (blending). */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x00u, 0x01000025u);	/* VIG0 stage 5, VIG1 stage 4 */
	reg(CTL0 + 0x04u, 0x01000025u);
	expect_rejected(&io, "pipes at different stages");

	/* 6. Fanout: VIG0 also staged on the second pair. */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x08u, LAYER_VIG23 | 5u);
	reg(CTL0 + 0x0cu, LAYER_VIG23 | 5u);
	expect_rejected(&io, "a pipe fanned out to two pairs");

	/* 7. Right mixer without staging identical to its left partner. */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x04u, LAYER_VIG23);
	expect_rejected(&io, "pair with different staging");

	/* 8. A right mixer with no left partner (LM0 unused). */
	mock_reset();
	mock_program();
	reg(CTL0 + 0x00u, 0u);
	expect_rejected(&io, "right LM without its left LM");

	/* 9. Scaling: VIG2 output narrower than its source. */
	mock_reset();
	mock_program();
	reg(VIG(2) + 0x0cu, (2000u << 16) | 799u);
	expect_rejected(&io, "scaled pipe");

	/* 10. Multirect on VIG3. */
	mock_reset();
	mock_program();
	reg(VIG(3) + 0x170u, 1u);
	expect_rejected(&io, "multirect pipe");

	/* 11. Source x not matching the output position. */
	mock_reset();
	mock_program();
	reg(VIG(2) + 0x08u, 2400u);
	reg(VIG(3) + 0x08u, 1600u);
	expect_rejected(&io, "source x not equal to output x");

	/* 12. A second active CTL. */
	mock_reset();
	mock_program();
	reg(CTL_OFF[1] + 0xf4u, 1u);
	expect_rejected(&io, "two active CTLs");

	/* 13. Routing changes between the two samples while the staged ID set
	 *     stays the same (LM1 loses its right-mixer bit). */
	mock_reset();
	mock_program();
	g_mock.unstable_off = LM(1) + 0x00u;
	g_mock.flip = 0x80000000u;
	expect_rejected(&io, "routing changed between samples");

	/* 14. A pipe register changes between the samples. */
	mock_reset();
	mock_program();
	g_mock.unstable_off = VIG(2) + 0x14u;
	g_mock.flip = 0x1000u;
	expect_rejected(&io, "pipe changed between samples");

	/* 15. Buffer outside cont_splash_region. */
	mock_reset();
	mock_program();
	for (unsigned n = 0; n < 4u; ++n)
		reg(VIG(n) + 0x14u, FB_BASE + FB_SIZE - 0x1000u);
	expect_rejected(&io, "buffer outside the splash region");

	/* 16. Malformed DT with a zero MDP base: rejected before any MMIO,
	 *     although every CTL/LM/SSPP offset fits the window. */
	build_pineapple(0u, MDP_SIZE);
	mock_reset();
	mock_program();
	g_mock.no_reads = true;
	expect_rejected(&io, "zero MDP base");
	assert(g_mock.reads == 0u);

	printf("tb710fu_splash_test: all cases passed\n");
	return 0;
}
