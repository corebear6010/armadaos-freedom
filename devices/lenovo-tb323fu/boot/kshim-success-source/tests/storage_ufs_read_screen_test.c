/* Host regression tests; link this file with src/ui/storage_ufs_read_screen.c. */
#include <storage_ufs_read_screen.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define GUARD_BYTES 64U
#define GUARD_VALUE 0xa5
#define FRAME_WIDTH 320U
#define FRAME_HEIGHT 320U
#define LINK_WIDTH 1024U
#define PIXEL_BYTES 4U
#define SMALL_WIDTH 12U
#define SMALL_HEIGHT 8U
#define STRIP_ROWS KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT
#define LINK_STRIP_ROWS KSHIM_UFS_READ_SCREEN_STRIP_HEIGHT_MAX
#define WHITE 0xffffffffU
#define BLACK 0xff000000U
#define UNTOUCHED 0x5a

static const char *test_name;
static size_t checks;

#define CHECK(expression) do { \
    ++checks; \
    if (!(expression)) { \
        fprintf(stderr, "%s:%d: %s: %s\n", \
                __FILE__, __LINE__, test_name, #expression); \
        abort(); \
    } \
} while (0)

typedef struct {
    kshim_framebuffer_t fb;
    kshim_framebuffer_config_t config;
    uint8_t *allocation;
    uint8_t *pixels;
    size_t size;
} fixture_t;

static void check_guards(const fixture_t *fixture)
{
    for (uint32_t i = 0; i < GUARD_BYTES; ++i) {
        CHECK(fixture->allocation[i] == GUARD_VALUE);
        CHECK(fixture->pixels[fixture->size + i] == GUARD_VALUE);
    }
}

/* Real framebuffer memory with poisoned bytes on both sides, so any store past
 * the configured frame is caught instead of silently landing in the heap. */
static void make_fixture(fixture_t *fixture, uint32_t width, uint32_t height)
{
    *fixture = (fixture_t){0};
    fixture->size = (size_t)width * PIXEL_BYTES * height;
    fixture->allocation = malloc(fixture->size + 2 * GUARD_BYTES);
    CHECK(fixture->allocation != NULL);
    memset(fixture->allocation, GUARD_VALUE, fixture->size + 2 * GUARD_BYTES);
    fixture->pixels = fixture->allocation + GUARD_BYTES;
    fixture->config = (kshim_framebuffer_config_t){
        .render_address = (uintptr_t)fixture->pixels,
        .width = width,
        .height = height,
        .stride = 0,
        .bpp = 32,
        .format = KSHIM_FB_FORMAT_XRGB8888,
        .foreground = WHITE,
        .background = BLACK,
        .buffer_size = fixture->size,
    };
    CHECK(kshim_fb_init(&fixture->fb, &fixture->config) == KSHIM_FB_OK);
    CHECK(fixture->fb.stride == width * PIXEL_BYTES);
    CHECK(fixture->fb.frame_size == fixture->size);
}

static void destroy_fixture(fixture_t *fixture)
{
    check_guards(fixture);
    free(fixture->allocation);
}

static void fill_frame(fixture_t *fixture, uint8_t value)
{
    memset(fixture->pixels, value, fixture->size);
}

static uint32_t pixel_at(const fixture_t *fixture, uint32_t x, uint32_t y)
{
    uint32_t value;
    memcpy(&value, fixture->pixels + (size_t)y * fixture->fb.stride +
                  (size_t)x * PIXEL_BYTES, sizeof(value));
    return value;
}

static size_t changed_bytes(const fixture_t *fixture, uint8_t value)
{
    size_t changed = 0;
    for (uint32_t y = 0; y < fixture->fb.height; ++y) {
        const uint8_t *row = fixture->pixels + (size_t)y * fixture->fb.stride;
        for (uint32_t byte = 0; byte < fixture->fb.width * PIXEL_BYTES; ++byte)
            if (row[byte] != value) ++changed;
    }
    return changed;
}

/* Compact copy of a diagnostic strip only, for before/after comparison. */
static uint8_t *copy_rows(const fixture_t *fixture, uint32_t rows, size_t *bytes)
{
    size_t row_bytes = (size_t)fixture->fb.width * PIXEL_BYTES;
    uint32_t bounded = fixture->fb.height < rows ? fixture->fb.height : rows;
    uint8_t *copy = malloc(row_bytes * bounded);
    CHECK(copy != NULL);
    for (uint32_t y = 0; y < bounded; ++y)
        memcpy(copy + (size_t)y * row_bytes,
               fixture->pixels + (size_t)y * fixture->fb.stride, row_bytes);
    *bytes = row_bytes * bounded;
    return copy;
}

static uint8_t *copy_strip(const fixture_t *fixture, size_t *bytes)
{
    return copy_rows(fixture, STRIP_ROWS, bytes);
}

static struct kshim_ufs_read_report sample_report(void)
{
    struct kshim_ufs_read_report report;
    memset(&report, 0, sizeof(report));
    report.version = KSHIM_UFS_READ_REPORT_VERSION;
    report.board = 1;
    report.stage = KSHIM_UFS_READ_STAGE_NOP;
    report.status = KSHIM_UFS_READ_UNRUN;
    report.release_status = KSHIM_STORAGE_OK;
    report.restore_verified = 1;
    report.handoff_safe = 1;
    report.block_size = 4096;
    report.block_count = 524288;
    report.dma_base = 0x12340000U;
    report.hcs = 0x10f;
    report.is = 0;
    report.doorbell = 1;
    report.ocs = 0xf;
    report.debug_valid = 7;
    return report;
}

static struct kshim_ufs_link_state sample_link(void)
{
    struct kshim_ufs_link_state link;
    memset(&link, 0, sizeof(link));
    link.initial_tx = 1;
    link.current_tx = 3;
    link.last_command = 0x18;
    link.command_result = 0;
    link.last_is = 0x400;
    link.power_status = 1;
    link.phase = 6;
    link.uncertain = 0;
    link.restore_hibern8 = 0;
    return link;
}

static void test_strip_only(void)
{
    fixture_t fixture;
    struct kshim_ufs_read_report report = sample_report();
    struct kshim_ufs_read_report before = report;
    size_t outside = 0, white = 0, black = 0;

    test_name = "strip only";
    make_fixture(&fixture, FRAME_WIDTH, FRAME_HEIGHT);
    fill_frame(&fixture, UNTOUCHED);
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_WRITE, 0x58, 1,
                                       NULL) ==
          KSHIM_FB_OK);
    /* The renderer is a reader of the report, never a writer. */
    CHECK(memcmp(&report, &before, sizeof(report)) == 0);

    for (uint32_t y = STRIP_ROWS; y < fixture.fb.height; ++y) {
        const uint8_t *row = fixture.pixels + (size_t)y * fixture.fb.stride;
        for (uint32_t byte = 0; byte < fixture.fb.width * PIXEL_BYTES; ++byte)
            if (row[byte] != UNTOUCHED) ++outside;
    }
    CHECK(outside == 0);

    for (uint32_t y = 0; y < STRIP_ROWS && y < fixture.fb.height; ++y)
        for (uint32_t x = 0; x < fixture.fb.width; ++x) {
            uint32_t value = pixel_at(&fixture, x, y);
            if (value == WHITE) ++white;
            else if (value == BLACK) ++black;
        }
    /* Something was drawn (glyphs) on a cleared background. */
    CHECK(white > 0);
    CHECK(black > 0);
    destroy_fixture(&fixture);
}

static void test_changes_with_report(void)
{
    fixture_t fixture;
    struct kshim_ufs_read_report report = sample_report();
    uint8_t *stage_a, *stage_b;
    size_t bytes_a = 0, bytes_b = 0;

    test_name = "report change";
    make_fixture(&fixture, FRAME_WIDTH, FRAME_HEIGHT);
    fill_frame(&fixture, UNTOUCHED);
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_READ, 0x30, 0,
                                       NULL) ==
          KSHIM_FB_OK);
    stage_a = copy_strip(&fixture, &bytes_a);

    report.stage = KSHIM_UFS_READ_STAGE_DONE;
    report.status = -10;
    report.release_status = -10;
    report.handoff_safe = 0;
    report.restore_verified = 0;
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_WRITE_DONE, 0x58,
                                       1, NULL) == KSHIM_FB_OK);
    stage_b = copy_strip(&fixture, &bytes_b);
    CHECK(bytes_a == bytes_b);
    CHECK(memcmp(stage_a, stage_b, bytes_a) != 0);
    free(stage_a);
    free(stage_b);
    destroy_fixture(&fixture);
}

/* A surface smaller than the strip must clip, not overflow. */
static void test_small_surface(void)
{
    fixture_t fixture;
    struct kshim_ufs_read_report report = sample_report();

    test_name = "small surface";
    make_fixture(&fixture, SMALL_WIDTH, SMALL_HEIGHT);
    fill_frame(&fixture, UNTOUCHED);
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_ENTRY, 0, 0, NULL) ==
          KSHIM_FB_OK);
    CHECK(changed_bytes(&fixture, UNTOUCHED) ==
          (size_t)SMALL_WIDTH * SMALL_HEIGHT * PIXEL_BYTES);
    destroy_fixture(&fixture);
}

static void test_invalid_arguments(void)
{
    fixture_t fixture;
    struct kshim_ufs_read_report report = sample_report();
    kshim_framebuffer_t broken;
    kshim_framebuffer_t empty = {0};
    kshim_framebuffer_t rejected = {0};
    kshim_framebuffer_config_t config;

    test_name = "invalid arguments";
    make_fixture(&fixture, FRAME_WIDTH, FRAME_HEIGHT);
    fill_frame(&fixture, UNTOUCHED);

    CHECK(kshim_ufs_read_screen_render(NULL, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, NULL,
                                       KSHIM_UFS_READ_SCREEN_ENTRY, 0, 0, NULL) ==
          KSHIM_FB_ERR_INVALID_ARGUMENT);
    CHECK(changed_bytes(&fixture, UNTOUCHED) == 0);

    broken = fixture.fb;
    broken.width = 0;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    broken = fixture.fb;
    broken.height = 0;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    broken = fixture.fb;
    broken.stride = 0;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    broken = fixture.fb;
    broken.frame_size = 0;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    broken = fixture.fb;
    broken.render_address = 0;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    broken = fixture.fb;
    broken.buffer_size = broken.frame_size - 1U;
    CHECK(kshim_ufs_read_screen_render(&broken, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_BUFFER_TOO_SMALL);
    CHECK(kshim_ufs_read_screen_render(&empty, &report, KSHIM_UFS_READ_SCREEN_ENTRY,
                                       0, 0, NULL) == KSHIM_FB_ERR_INVALID_ARGUMENT);
    CHECK(changed_bytes(&fixture, UNTOUCHED) == 0);

    /* A configuration the framebuffer itself rejects must not become drawable. */
    config = fixture.config;
    config.bpp = 16;
    CHECK(kshim_fb_init(&rejected, &config) != KSHIM_FB_OK);
    CHECK(kshim_ufs_read_screen_render(&rejected, &report,
                                       KSHIM_UFS_READ_SCREEN_ENTRY, 0, 0, NULL) < 0);
    destroy_fixture(&fixture);
}

/* The link profile grows the strip to ten lines (328 rows) without touching
 * anything below it, and its two extra lines are actually painted. */
static void test_link_strip(void)
{
    fixture_t fixture;
    struct kshim_ufs_read_report report = sample_report();
    struct kshim_ufs_link_state link = sample_link();
    struct kshim_ufs_link_state before = link;
    struct kshim_ufs_link_state changed;
    uint8_t *stage_a, *stage_b;
    size_t bytes_a = 0, bytes_b = 0;
    size_t outside = 0, white_below = 0;

    test_name = "link strip";
    make_fixture(&fixture, LINK_WIDTH, LINK_STRIP_ROWS + 32);
    fill_frame(&fixture, UNTOUCHED);
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_ENTRY, 0, 0,
                                       &link) == KSHIM_FB_OK);
    /* The renderer displays the link state but never mutates it. */
    CHECK(memcmp(&link, &before, sizeof(link)) == 0);
    stage_a = copy_rows(&fixture, LINK_STRIP_ROWS, &bytes_a);

    for (uint32_t y = LINK_STRIP_ROWS; y < fixture.fb.height; ++y) {
        const uint8_t *row = fixture.pixels + (size_t)y * fixture.fb.stride;
        for (uint32_t byte = 0; byte < fixture.fb.width * PIXEL_BYTES; ++byte)
            if (row[byte] != UNTOUCHED) ++outside;
    }
    CHECK(outside == 0);

    /* Rows below 200 contain the link, device power and SCSI lines. */
    for (uint32_t y = STRIP_ROWS; y < LINK_STRIP_ROWS; ++y)
        for (uint32_t x = 0; x < fixture.fb.width; ++x)
            if (pixel_at(&fixture, x, y) == WHITE) ++white_below;
    CHECK(white_below > 0);

    link.phase = 4;
    link.current_tx = 1;
    changed = link;
    CHECK(kshim_ufs_read_screen_render(&fixture.fb, &report,
                                       KSHIM_UFS_READ_SCREEN_WRITE_DONE, 0x58,
                                       1, &link) == KSHIM_FB_OK);
    CHECK(memcmp(&link, &changed, sizeof(link)) == 0);
    stage_b = copy_rows(&fixture, LINK_STRIP_ROWS, &bytes_b);
    CHECK(bytes_a == bytes_b);
    CHECK(memcmp(stage_a, stage_b, bytes_a) != 0);
    free(stage_a);
    free(stage_b);
    destroy_fixture(&fixture);
}

static void test_power_lines_select_saved_state(void)
{
    fixture_t f;struct kshim_ufs_read_report r=sample_report(),before;
    struct kshim_ufs_link_state link=sample_link();
    test_name="power evidence lines";
    make_fixture(&f,LINK_WIDTH,LINK_STRIP_ROWS+32);
    fill_frame(&f,UNTOUCHED);
    r.power_before_release=(struct kshim_ufs_power_state){0x22,0x11,3,0,2};
    r.scsi_before_release=(struct kshim_ufs_scsi_state){0x88,0,2,2,4,2,3};
    before=r;
    CHECK(kshim_ufs_read_screen_render(&f.fb,&r,1,0,0,&link)==0);
    CHECK(memcmp(&r,&before,sizeof(r))==0);
    const size_t off=264u*f.fb.stride,bytes=64u*f.fb.stride;
    unsigned char *saved=malloc(bytes);CHECK(saved!=NULL);
    memcpy(saved,f.pixels+off,bytes);
    /* A successful read selects the after-release fields. Identical evidence
     * renders identically in these two lines despite the top status change. */
    r.status=0;r.power_after_release=r.power_before_release;
    r.scsi_after_release=r.scsi_before_release;
    CHECK(kshim_ufs_read_screen_render(&f.fb,&r,1,0,0,&link)==0);
    CHECK(memcmp(saved,f.pixels+off,bytes)==0);
    r.power_after_release.current_mode=0x22;r.power_after_release.phase=7;
    r.scsi_after_release.ascq=7;
    CHECK(kshim_ufs_read_screen_render(&f.fb,&r,1,0,0,&link)==0);
    CHECK(memcmp(saved,f.pixels+off,bytes)!=0);
    free(saved);destroy_fixture(&f);
}
int main(void)
{
    test_strip_only();
    test_changes_with_report();
    test_small_surface();
    test_invalid_arguments();
    test_link_strip();
    test_power_lines_select_saved_state();
    printf("storage_ufs_read_screen: strip-only rendering, report immutability, "
           "clipped surfaces, invalid arguments and link strip passed (%zu checks)\n",
           checks);
    return 0;
}
