/*
 * UTF-8 terminal-cell overlay for kilix-playalong.
 *
 * kitty-framebuffer puts its image behind the text plane, so the terminal's
 * own cells are the only place a lyric can be drawn without going through
 * soft-raster's ASCII bitmap font.  Everything here therefore has to agree
 * with the terminal about two things: which byte sequences are legal UTF-8,
 * and how many columns each of them advances the cursor.  Disagreeing about
 * the second one does not draw a wrong glyph, it desynchronises the cursor
 * and corrupts every row drawn afterwards, which is why the width tables
 * below are generated rather than guessed.
 *
 * Nothing derived from the text is ever emitted as a control sequence: the
 * fit/scan walk refuses C0, DEL and C1 outright, so the bytes copied into
 * the output buffer cannot be read by the terminal as a command.
 */
#include "kilix_playalong/kpa_cells.h"

#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct cell_range {
    uint32_t lo;
    uint32_t hi;
} cell_range;

/*
 * Width tables, generated from Python 3.13's unicodedata module, which is
 * Unicode 15.1.0:
 *
 *   wide_ranges       East_Asian_Width of W or F, plus the unassigned code
 *                     points inside the CJK blocks that EastAsianWidth.txt
 *                     gives a default of W (U+3400..U+4DBF, U+4E00..U+9FFF,
 *                     U+F900..U+FAFF, U+20000..U+2FFFD, U+30000..U+3FFFD).
 *   zero_width_ranges general category Mn or Me, plus the enumerated
 *                     zero-advance format characters U+061C, U+180E,
 *                     U+200B..U+200F, U+202A..U+202E, U+2060..U+2064,
 *                     U+2066..U+206F, U+FEFF, U+FFF9..U+FFFB,
 *                     U+1D173..U+1D17A, U+E0001 and U+E0020..U+E007F.
 *
 * Where the approximation ends, stated plainly rather than implied away:
 *
 *   - There is no grapheme clustering.  Width is summed per code point, so
 *     an emoji ZWJ sequence measures as the sum of its parts (a two-person
 *     family emoji reports 4, not 2) and a regional-indicator flag reports
 *     2 because each half is narrow.  Terminals disagree with each other
 *     about clusters; per code point is the only answer that is
 *     reproducible, and it is what kpa_cells_fit is written against.
 *   - The rest of category Cf -- U+00AD and the Arabic and Kaithi prepended
 *     concatenation marks -- counts one column, matching xterm and kitty
 *     rather than counting zero.
 *   - Category Mc, the spacing combining marks such as U+0903, counts one
 *     column.  It is spacing by definition, though some fonts draw it
 *     inside the base cell.
 *   - Unassigned code points outside those CJK blocks count one column.  A
 *     later Unicode release can assign a wide character there, and this
 *     table is one column short for it until it is regenerated.
 *   - U+3099 and U+309A are both Mn and East_Asian_Width=W.  The zero-width
 *     table is consulted first, so they measure zero, which is what
 *     terminals do with them.
 */
static const cell_range zero_width_ranges[] = {
    {0x0300u, 0x036Fu}, {0x0483u, 0x0489u}, {0x0591u, 0x05BDu},
    {0x05BFu, 0x05BFu}, {0x05C1u, 0x05C2u}, {0x05C4u, 0x05C5u},
    {0x05C7u, 0x05C7u}, {0x0610u, 0x061Au}, {0x061Cu, 0x061Cu},
    {0x064Bu, 0x065Fu}, {0x0670u, 0x0670u}, {0x06D6u, 0x06DCu},
    {0x06DFu, 0x06E4u}, {0x06E7u, 0x06E8u}, {0x06EAu, 0x06EDu},
    {0x0711u, 0x0711u}, {0x0730u, 0x074Au}, {0x07A6u, 0x07B0u},
    {0x07EBu, 0x07F3u}, {0x07FDu, 0x07FDu}, {0x0816u, 0x0819u},
    {0x081Bu, 0x0823u}, {0x0825u, 0x0827u}, {0x0829u, 0x082Du},
    {0x0859u, 0x085Bu}, {0x0898u, 0x089Fu}, {0x08CAu, 0x08E1u},
    {0x08E3u, 0x0902u}, {0x093Au, 0x093Au}, {0x093Cu, 0x093Cu},
    {0x0941u, 0x0948u}, {0x094Du, 0x094Du}, {0x0951u, 0x0957u},
    {0x0962u, 0x0963u}, {0x0981u, 0x0981u}, {0x09BCu, 0x09BCu},
    {0x09C1u, 0x09C4u}, {0x09CDu, 0x09CDu}, {0x09E2u, 0x09E3u},
    {0x09FEu, 0x09FEu}, {0x0A01u, 0x0A02u}, {0x0A3Cu, 0x0A3Cu},
    {0x0A41u, 0x0A42u}, {0x0A47u, 0x0A48u}, {0x0A4Bu, 0x0A4Du},
    {0x0A51u, 0x0A51u}, {0x0A70u, 0x0A71u}, {0x0A75u, 0x0A75u},
    {0x0A81u, 0x0A82u}, {0x0ABCu, 0x0ABCu}, {0x0AC1u, 0x0AC5u},
    {0x0AC7u, 0x0AC8u}, {0x0ACDu, 0x0ACDu}, {0x0AE2u, 0x0AE3u},
    {0x0AFAu, 0x0AFFu}, {0x0B01u, 0x0B01u}, {0x0B3Cu, 0x0B3Cu},
    {0x0B3Fu, 0x0B3Fu}, {0x0B41u, 0x0B44u}, {0x0B4Du, 0x0B4Du},
    {0x0B55u, 0x0B56u}, {0x0B62u, 0x0B63u}, {0x0B82u, 0x0B82u},
    {0x0BC0u, 0x0BC0u}, {0x0BCDu, 0x0BCDu}, {0x0C00u, 0x0C00u},
    {0x0C04u, 0x0C04u}, {0x0C3Cu, 0x0C3Cu}, {0x0C3Eu, 0x0C40u},
    {0x0C46u, 0x0C48u}, {0x0C4Au, 0x0C4Du}, {0x0C55u, 0x0C56u},
    {0x0C62u, 0x0C63u}, {0x0C81u, 0x0C81u}, {0x0CBCu, 0x0CBCu},
    {0x0CBFu, 0x0CBFu}, {0x0CC6u, 0x0CC6u}, {0x0CCCu, 0x0CCDu},
    {0x0CE2u, 0x0CE3u}, {0x0D00u, 0x0D01u}, {0x0D3Bu, 0x0D3Cu},
    {0x0D41u, 0x0D44u}, {0x0D4Du, 0x0D4Du}, {0x0D62u, 0x0D63u},
    {0x0D81u, 0x0D81u}, {0x0DCAu, 0x0DCAu}, {0x0DD2u, 0x0DD4u},
    {0x0DD6u, 0x0DD6u}, {0x0E31u, 0x0E31u}, {0x0E34u, 0x0E3Au},
    {0x0E47u, 0x0E4Eu}, {0x0EB1u, 0x0EB1u}, {0x0EB4u, 0x0EBCu},
    {0x0EC8u, 0x0ECEu}, {0x0F18u, 0x0F19u}, {0x0F35u, 0x0F35u},
    {0x0F37u, 0x0F37u}, {0x0F39u, 0x0F39u}, {0x0F71u, 0x0F7Eu},
    {0x0F80u, 0x0F84u}, {0x0F86u, 0x0F87u}, {0x0F8Du, 0x0F97u},
    {0x0F99u, 0x0FBCu}, {0x0FC6u, 0x0FC6u}, {0x102Du, 0x1030u},
    {0x1032u, 0x1037u}, {0x1039u, 0x103Au}, {0x103Du, 0x103Eu},
    {0x1058u, 0x1059u}, {0x105Eu, 0x1060u}, {0x1071u, 0x1074u},
    {0x1082u, 0x1082u}, {0x1085u, 0x1086u}, {0x108Du, 0x108Du},
    {0x109Du, 0x109Du}, {0x135Du, 0x135Fu}, {0x1712u, 0x1714u},
    {0x1732u, 0x1733u}, {0x1752u, 0x1753u}, {0x1772u, 0x1773u},
    {0x17B4u, 0x17B5u}, {0x17B7u, 0x17BDu}, {0x17C6u, 0x17C6u},
    {0x17C9u, 0x17D3u}, {0x17DDu, 0x17DDu}, {0x180Bu, 0x180Fu},
    {0x1885u, 0x1886u}, {0x18A9u, 0x18A9u}, {0x1920u, 0x1922u},
    {0x1927u, 0x1928u}, {0x1932u, 0x1932u}, {0x1939u, 0x193Bu},
    {0x1A17u, 0x1A18u}, {0x1A1Bu, 0x1A1Bu}, {0x1A56u, 0x1A56u},
    {0x1A58u, 0x1A5Eu}, {0x1A60u, 0x1A60u}, {0x1A62u, 0x1A62u},
    {0x1A65u, 0x1A6Cu}, {0x1A73u, 0x1A7Cu}, {0x1A7Fu, 0x1A7Fu},
    {0x1AB0u, 0x1ACEu}, {0x1B00u, 0x1B03u}, {0x1B34u, 0x1B34u},
    {0x1B36u, 0x1B3Au}, {0x1B3Cu, 0x1B3Cu}, {0x1B42u, 0x1B42u},
    {0x1B6Bu, 0x1B73u}, {0x1B80u, 0x1B81u}, {0x1BA2u, 0x1BA5u},
    {0x1BA8u, 0x1BA9u}, {0x1BABu, 0x1BADu}, {0x1BE6u, 0x1BE6u},
    {0x1BE8u, 0x1BE9u}, {0x1BEDu, 0x1BEDu}, {0x1BEFu, 0x1BF1u},
    {0x1C2Cu, 0x1C33u}, {0x1C36u, 0x1C37u}, {0x1CD0u, 0x1CD2u},
    {0x1CD4u, 0x1CE0u}, {0x1CE2u, 0x1CE8u}, {0x1CEDu, 0x1CEDu},
    {0x1CF4u, 0x1CF4u}, {0x1CF8u, 0x1CF9u}, {0x1DC0u, 0x1DFFu},
    {0x200Bu, 0x200Fu}, {0x202Au, 0x202Eu}, {0x2060u, 0x2064u},
    {0x2066u, 0x206Fu}, {0x20D0u, 0x20F0u}, {0x2CEFu, 0x2CF1u},
    {0x2D7Fu, 0x2D7Fu}, {0x2DE0u, 0x2DFFu}, {0x302Au, 0x302Du},
    {0x3099u, 0x309Au}, {0xA66Fu, 0xA672u}, {0xA674u, 0xA67Du},
    {0xA69Eu, 0xA69Fu}, {0xA6F0u, 0xA6F1u}, {0xA802u, 0xA802u},
    {0xA806u, 0xA806u}, {0xA80Bu, 0xA80Bu}, {0xA825u, 0xA826u},
    {0xA82Cu, 0xA82Cu}, {0xA8C4u, 0xA8C5u}, {0xA8E0u, 0xA8F1u},
    {0xA8FFu, 0xA8FFu}, {0xA926u, 0xA92Du}, {0xA947u, 0xA951u},
    {0xA980u, 0xA982u}, {0xA9B3u, 0xA9B3u}, {0xA9B6u, 0xA9B9u},
    {0xA9BCu, 0xA9BDu}, {0xA9E5u, 0xA9E5u}, {0xAA29u, 0xAA2Eu},
    {0xAA31u, 0xAA32u}, {0xAA35u, 0xAA36u}, {0xAA43u, 0xAA43u},
    {0xAA4Cu, 0xAA4Cu}, {0xAA7Cu, 0xAA7Cu}, {0xAAB0u, 0xAAB0u},
    {0xAAB2u, 0xAAB4u}, {0xAAB7u, 0xAAB8u}, {0xAABEu, 0xAABFu},
    {0xAAC1u, 0xAAC1u}, {0xAAECu, 0xAAEDu}, {0xAAF6u, 0xAAF6u},
    {0xABE5u, 0xABE5u}, {0xABE8u, 0xABE8u}, {0xABEDu, 0xABEDu},
    {0xFB1Eu, 0xFB1Eu}, {0xFE00u, 0xFE0Fu}, {0xFE20u, 0xFE2Fu},
    {0xFEFFu, 0xFEFFu}, {0xFFF9u, 0xFFFBu}, {0x101FDu, 0x101FDu},
    {0x102E0u, 0x102E0u}, {0x10376u, 0x1037Au}, {0x10A01u, 0x10A03u},
    {0x10A05u, 0x10A06u}, {0x10A0Cu, 0x10A0Fu}, {0x10A38u, 0x10A3Au},
    {0x10A3Fu, 0x10A3Fu}, {0x10AE5u, 0x10AE6u}, {0x10D24u, 0x10D27u},
    {0x10EABu, 0x10EACu}, {0x10EFDu, 0x10EFFu}, {0x10F46u, 0x10F50u},
    {0x10F82u, 0x10F85u}, {0x11001u, 0x11001u}, {0x11038u, 0x11046u},
    {0x11070u, 0x11070u}, {0x11073u, 0x11074u}, {0x1107Fu, 0x11081u},
    {0x110B3u, 0x110B6u}, {0x110B9u, 0x110BAu}, {0x110C2u, 0x110C2u},
    {0x11100u, 0x11102u}, {0x11127u, 0x1112Bu}, {0x1112Du, 0x11134u},
    {0x11173u, 0x11173u}, {0x11180u, 0x11181u}, {0x111B6u, 0x111BEu},
    {0x111C9u, 0x111CCu}, {0x111CFu, 0x111CFu}, {0x1122Fu, 0x11231u},
    {0x11234u, 0x11234u}, {0x11236u, 0x11237u}, {0x1123Eu, 0x1123Eu},
    {0x11241u, 0x11241u}, {0x112DFu, 0x112DFu}, {0x112E3u, 0x112EAu},
    {0x11300u, 0x11301u}, {0x1133Bu, 0x1133Cu}, {0x11340u, 0x11340u},
    {0x11366u, 0x1136Cu}, {0x11370u, 0x11374u}, {0x11438u, 0x1143Fu},
    {0x11442u, 0x11444u}, {0x11446u, 0x11446u}, {0x1145Eu, 0x1145Eu},
    {0x114B3u, 0x114B8u}, {0x114BAu, 0x114BAu}, {0x114BFu, 0x114C0u},
    {0x114C2u, 0x114C3u}, {0x115B2u, 0x115B5u}, {0x115BCu, 0x115BDu},
    {0x115BFu, 0x115C0u}, {0x115DCu, 0x115DDu}, {0x11633u, 0x1163Au},
    {0x1163Du, 0x1163Du}, {0x1163Fu, 0x11640u}, {0x116ABu, 0x116ABu},
    {0x116ADu, 0x116ADu}, {0x116B0u, 0x116B5u}, {0x116B7u, 0x116B7u},
    {0x1171Du, 0x1171Fu}, {0x11722u, 0x11725u}, {0x11727u, 0x1172Bu},
    {0x1182Fu, 0x11837u}, {0x11839u, 0x1183Au}, {0x1193Bu, 0x1193Cu},
    {0x1193Eu, 0x1193Eu}, {0x11943u, 0x11943u}, {0x119D4u, 0x119D7u},
    {0x119DAu, 0x119DBu}, {0x119E0u, 0x119E0u}, {0x11A01u, 0x11A0Au},
    {0x11A33u, 0x11A38u}, {0x11A3Bu, 0x11A3Eu}, {0x11A47u, 0x11A47u},
    {0x11A51u, 0x11A56u}, {0x11A59u, 0x11A5Bu}, {0x11A8Au, 0x11A96u},
    {0x11A98u, 0x11A99u}, {0x11C30u, 0x11C36u}, {0x11C38u, 0x11C3Du},
    {0x11C3Fu, 0x11C3Fu}, {0x11C92u, 0x11CA7u}, {0x11CAAu, 0x11CB0u},
    {0x11CB2u, 0x11CB3u}, {0x11CB5u, 0x11CB6u}, {0x11D31u, 0x11D36u},
    {0x11D3Au, 0x11D3Au}, {0x11D3Cu, 0x11D3Du}, {0x11D3Fu, 0x11D45u},
    {0x11D47u, 0x11D47u}, {0x11D90u, 0x11D91u}, {0x11D95u, 0x11D95u},
    {0x11D97u, 0x11D97u}, {0x11EF3u, 0x11EF4u}, {0x11F00u, 0x11F01u},
    {0x11F36u, 0x11F3Au}, {0x11F40u, 0x11F40u}, {0x11F42u, 0x11F42u},
    {0x13440u, 0x13440u}, {0x13447u, 0x13455u}, {0x16AF0u, 0x16AF4u},
    {0x16B30u, 0x16B36u}, {0x16F4Fu, 0x16F4Fu}, {0x16F8Fu, 0x16F92u},
    {0x16FE4u, 0x16FE4u}, {0x1BC9Du, 0x1BC9Eu}, {0x1CF00u, 0x1CF2Du},
    {0x1CF30u, 0x1CF46u}, {0x1D167u, 0x1D169u}, {0x1D173u, 0x1D182u},
    {0x1D185u, 0x1D18Bu}, {0x1D1AAu, 0x1D1ADu}, {0x1D242u, 0x1D244u},
    {0x1DA00u, 0x1DA36u}, {0x1DA3Bu, 0x1DA6Cu}, {0x1DA75u, 0x1DA75u},
    {0x1DA84u, 0x1DA84u}, {0x1DA9Bu, 0x1DA9Fu}, {0x1DAA1u, 0x1DAAFu},
    {0x1E000u, 0x1E006u}, {0x1E008u, 0x1E018u}, {0x1E01Bu, 0x1E021u},
    {0x1E023u, 0x1E024u}, {0x1E026u, 0x1E02Au}, {0x1E08Fu, 0x1E08Fu},
    {0x1E130u, 0x1E136u}, {0x1E2AEu, 0x1E2AEu}, {0x1E2ECu, 0x1E2EFu},
    {0x1E4ECu, 0x1E4EFu}, {0x1E8D0u, 0x1E8D6u}, {0x1E944u, 0x1E94Au},
    {0xE0001u, 0xE0001u}, {0xE0020u, 0xE007Fu}, {0xE0100u, 0xE01EFu},
};

static const cell_range wide_ranges[] = {
    {0x1100u, 0x115Fu}, {0x231Au, 0x231Bu}, {0x2329u, 0x232Au},
    {0x23E9u, 0x23ECu}, {0x23F0u, 0x23F0u}, {0x23F3u, 0x23F3u},
    {0x25FDu, 0x25FEu}, {0x2614u, 0x2615u}, {0x2648u, 0x2653u},
    {0x267Fu, 0x267Fu}, {0x2693u, 0x2693u}, {0x26A1u, 0x26A1u},
    {0x26AAu, 0x26ABu}, {0x26BDu, 0x26BEu}, {0x26C4u, 0x26C5u},
    {0x26CEu, 0x26CEu}, {0x26D4u, 0x26D4u}, {0x26EAu, 0x26EAu},
    {0x26F2u, 0x26F3u}, {0x26F5u, 0x26F5u}, {0x26FAu, 0x26FAu},
    {0x26FDu, 0x26FDu}, {0x2705u, 0x2705u}, {0x270Au, 0x270Bu},
    {0x2728u, 0x2728u}, {0x274Cu, 0x274Cu}, {0x274Eu, 0x274Eu},
    {0x2753u, 0x2755u}, {0x2757u, 0x2757u}, {0x2795u, 0x2797u},
    {0x27B0u, 0x27B0u}, {0x27BFu, 0x27BFu}, {0x2B1Bu, 0x2B1Cu},
    {0x2B50u, 0x2B50u}, {0x2B55u, 0x2B55u}, {0x2E80u, 0x2E99u},
    {0x2E9Bu, 0x2EF3u}, {0x2F00u, 0x2FD5u}, {0x2FF0u, 0x303Eu},
    {0x3041u, 0x3096u}, {0x3099u, 0x30FFu}, {0x3105u, 0x312Fu},
    {0x3131u, 0x318Eu}, {0x3190u, 0x31E3u}, {0x31EFu, 0x321Eu},
    {0x3220u, 0x3247u}, {0x3250u, 0x4DBFu}, {0x4E00u, 0xA48Cu},
    {0xA490u, 0xA4C6u}, {0xA960u, 0xA97Cu}, {0xAC00u, 0xD7A3u},
    {0xF900u, 0xFAFFu}, {0xFE10u, 0xFE19u}, {0xFE30u, 0xFE52u},
    {0xFE54u, 0xFE66u}, {0xFE68u, 0xFE6Bu}, {0xFF01u, 0xFF60u},
    {0xFFE0u, 0xFFE6u}, {0x16FE0u, 0x16FE4u}, {0x16FF0u, 0x16FF1u},
    {0x17000u, 0x187F7u}, {0x18800u, 0x18CD5u}, {0x18D00u, 0x18D08u},
    {0x1AFF0u, 0x1AFF3u}, {0x1AFF5u, 0x1AFFBu}, {0x1AFFDu, 0x1AFFEu},
    {0x1B000u, 0x1B122u}, {0x1B132u, 0x1B132u}, {0x1B150u, 0x1B152u},
    {0x1B155u, 0x1B155u}, {0x1B164u, 0x1B167u}, {0x1B170u, 0x1B2FBu},
    {0x1F004u, 0x1F004u}, {0x1F0CFu, 0x1F0CFu}, {0x1F18Eu, 0x1F18Eu},
    {0x1F191u, 0x1F19Au}, {0x1F200u, 0x1F202u}, {0x1F210u, 0x1F23Bu},
    {0x1F240u, 0x1F248u}, {0x1F250u, 0x1F251u}, {0x1F260u, 0x1F265u},
    {0x1F300u, 0x1F320u}, {0x1F32Du, 0x1F335u}, {0x1F337u, 0x1F37Cu},
    {0x1F37Eu, 0x1F393u}, {0x1F3A0u, 0x1F3CAu}, {0x1F3CFu, 0x1F3D3u},
    {0x1F3E0u, 0x1F3F0u}, {0x1F3F4u, 0x1F3F4u}, {0x1F3F8u, 0x1F43Eu},
    {0x1F440u, 0x1F440u}, {0x1F442u, 0x1F4FCu}, {0x1F4FFu, 0x1F53Du},
    {0x1F54Bu, 0x1F54Eu}, {0x1F550u, 0x1F567u}, {0x1F57Au, 0x1F57Au},
    {0x1F595u, 0x1F596u}, {0x1F5A4u, 0x1F5A4u}, {0x1F5FBu, 0x1F64Fu},
    {0x1F680u, 0x1F6C5u}, {0x1F6CCu, 0x1F6CCu}, {0x1F6D0u, 0x1F6D2u},
    {0x1F6D5u, 0x1F6D7u}, {0x1F6DCu, 0x1F6DFu}, {0x1F6EBu, 0x1F6ECu},
    {0x1F6F4u, 0x1F6FCu}, {0x1F7E0u, 0x1F7EBu}, {0x1F7F0u, 0x1F7F0u},
    {0x1F90Cu, 0x1F93Au}, {0x1F93Cu, 0x1F945u}, {0x1F947u, 0x1F9FFu},
    {0x1FA70u, 0x1FA7Cu}, {0x1FA80u, 0x1FA88u}, {0x1FA90u, 0x1FABDu},
    {0x1FABFu, 0x1FAC5u}, {0x1FACEu, 0x1FADBu}, {0x1FAE0u, 0x1FAE8u},
    {0x1FAF0u, 0x1FAF8u}, {0x20000u, 0x2FFFDu}, {0x30000u, 0x3FFFDu},
};

static bool in_ranges(const cell_range *ranges, size_t count, uint32_t code)
{
    size_t low = 0u;
    size_t high = count;

    while (low < high) {
        const size_t middle = low + (high - low) / 2u;
        if (code < ranges[middle].lo) high = middle;
        else if (code > ranges[middle].hi) low = middle + 1u;
        else return true;
    }
    return false;
}

/* -1 means "must not reach the terminal", not "unknown width". */
static int code_width(uint32_t code)
{
    if (code < 0x20u || (code >= 0x7Fu && code <= 0x9Fu)) return -1;
    if (in_ranges(zero_width_ranges,
                  sizeof zero_width_ranges / sizeof zero_width_ranges[0],
                  code)) return 0;
    if (in_ranges(wide_ranges, sizeof wide_ranges / sizeof wide_ranges[0],
                  code)) return 2;
    return 1;
}

typedef struct scan_step {
    size_t bytes;   /* 0 when the sequence is malformed or truncated */
    int width;      /* -1 when the code point is refused */
    uint32_t code;
} scan_step;

static size_t sequence_length(unsigned char lead)
{
    if (lead < 0x80u) return 1u;
    if (lead >= 0xC2u && lead <= 0xDFu) return 2u;
    if (lead >= 0xE0u && lead <= 0xEFu) return 3u;
    if (lead >= 0xF0u && lead <= 0xF4u) return 4u;
    return 0u;   /* 0x80..0xC1 and 0xF5..0xFF never lead a sequence */
}

/*
 * Unicode 15.1.0 table 3-7, "Well-Formed UTF-8 Byte Sequences".  The
 * narrowed second-byte bounds are the whole trick: they are what reject the
 * overlong forms (0xC0/0xC1 as a lead, E0 80.., F0 80..), the UTF-16
 * surrogates (ED A0..ED BF) and everything past U+10FFFF (F4 90.., F5..FF).
 * Accepting exactly this set is accepting exactly what Python's
 * str.encode("utf-8") emits and its decoder will take back, which is the
 * standard the fixture generator on the other side of this contract holds
 * us to.
 */
static scan_step decode_step(const unsigned char *text, size_t length,
                             size_t offset)
{
    static const unsigned char lead_mask[5] = {
        0u, 0x7Fu, 0x1Fu, 0x0Fu, 0x07u
    };
    scan_step step;
    unsigned char lead;
    unsigned char low = 0x80u;
    unsigned char high = 0xBFu;
    size_t needed;
    uint32_t code;

    step.bytes = 0u;
    step.width = -1;
    step.code = 0u;
    if (text == NULL || offset >= length) return step;
    lead = text[offset];
    needed = sequence_length(lead);
    /* Bounds before arithmetic: length - offset is at least 1 here. */
    if (needed == 0u || needed > length - offset) return step;
    if (lead == 0xE0u) low = 0xA0u;
    else if (lead == 0xEDu) high = 0x9Fu;
    else if (lead == 0xF0u) low = 0x90u;
    else if (lead == 0xF4u) high = 0x8Fu;
    if (needed > 1u &&
        (text[offset + 1u] < low || text[offset + 1u] > high)) return step;
    for (size_t index = 2u; index < needed; ++index) {
        const unsigned char byte = text[offset + index];
        if (byte < 0x80u || byte > 0xBFu) return step;
    }
    code = (uint32_t)(lead & lead_mask[needed]);
    for (size_t index = 1u; index < needed; ++index)
        code = (code << 6) | (uint32_t)(text[offset + index] & 0x3Fu);
    step.bytes = needed;
    step.code = code;
    step.width = code_width(code);
    return step;
}

/*
 * ZWNJ and ZWJ modify what follows them, so a truncated prefix must not end
 * on one; a combining mark modifies what precedes it and is safe to keep.
 */
static bool needs_following_base(uint32_t code)
{
    return code == 0x200Cu || code == 0x200Du;
}

/*
 * Encoding validity only.  A lyric line holding a raw ESC is well-formed
 * UTF-8 and this returns true for it; refusing it is kpa_cells_width's job,
 * and the two questions are kept apart on purpose.
 */
bool kpa_cells_valid_utf8(const char *text, size_t length)
{
    const unsigned char *bytes = (const unsigned char *)text;
    size_t offset = 0u;

    if (text == NULL) return length == 0u;
    while (offset < length) {
        const scan_step step = decode_step(bytes, length, offset);
        if (step.bytes == 0u) return false;
        offset += step.bytes;
    }
    return true;
}

int kpa_cells_width(const char *text, size_t length)
{
    const unsigned char *bytes = (const unsigned char *)text;
    size_t offset = 0u;
    uint64_t total = 0u;

    if (text == NULL) return length == 0u ? 0 : -1;
    while (offset < length) {
        const scan_step step = decode_step(bytes, length, offset);
        if (step.bytes == 0u || step.width < 0) return -1;
        total += (uint64_t)step.width;
        offset += step.bytes;
    }
    /* A string too wide to count is a broken caller, not a 2GB lyric. */
    return total > (uint64_t)INT_MAX ? -1 : (int)total;
}

size_t kpa_cells_fit(const char *text, size_t length, int columns)
{
    const unsigned char *bytes = (const unsigned char *)text;
    size_t offset = 0u;
    size_t accepted = 0u;
    int used = 0;

    if (text == NULL || columns <= 0) return 0u;
    while (offset < length) {
        const scan_step step = decode_step(bytes, length, offset);

        if (step.bytes == 0u || step.width < 0) break;
        if (step.width > columns - used) break;
        used += step.width;
        offset += step.bytes;
        /* Zero-width marks ride along with the base already accepted. */
        if (!needs_following_base(step.code)) accepted = offset;
    }
    return accepted;
}

enum {
    output_capacity = 8192,
    /* Bounds the decimal digits a coordinate can contribute. */
    max_coordinate = 32767
};

struct kpa_cells_writer {
    int fd;
    bool in_frame;
    bool failed;    /* a write(2) failed; stop touching the terminal */
    size_t used;
    unsigned char buffer[output_capacity];
};

static void flush_buffer(kpa_cells_writer *writer)
{
    size_t offset = 0u;

    if (writer->failed) {
        writer->used = 0u;
        return;
    }
    while (offset < writer->used) {
        const ssize_t count = write(writer->fd, writer->buffer + offset,
                                    writer->used - offset);
        if (count > 0) offset += (size_t)count;
        else if (count < 0 && errno == EINTR) continue;
        else {
            writer->failed = true;
            break;
        }
    }
    writer->used = 0u;
}

static void emit_raw(kpa_cells_writer *writer, const char *bytes,
                     size_t count)
{
    if (writer->failed || count == 0u) return;
    if (count > (size_t)output_capacity - writer->used) {
        flush_buffer(writer);
        if (writer->failed) return;
    }
    /* No control sequence here is anywhere near 8 KiB; drop rather than
       emit half of one if that ever stops being true. */
    if (count > (size_t)output_capacity - writer->used) return;
    (void)memcpy(writer->buffer + writer->used, bytes, count);
    writer->used += count;
}

static void emit_literal(kpa_cells_writer *writer, const char *literal)
{
    emit_raw(writer, literal, strlen(literal));
}

static void emit_number(kpa_cells_writer *writer, uint32_t value)
{
    char digits[11];
    size_t index = sizeof digits;

    do {
        digits[--index] = (char)('0' + (value % 10u));
        value /= 10u;
    } while (value != 0u && index > 0u);
    emit_raw(writer, digits + index, sizeof digits - index);
}

static uint32_t clamp_coordinate(int value)
{
    if (value < 1) return 1u;
    if (value > max_coordinate) return (uint32_t)max_coordinate;
    return (uint32_t)value;
}

/* Longest prefix of `text` that fits in `budget` bytes on a boundary. */
static size_t fit_bytes(const char *text, size_t length, size_t budget)
{
    const unsigned char *bytes = (const unsigned char *)text;
    size_t offset = 0u;
    size_t accepted = 0u;

    while (offset < length && offset < budget) {
        const scan_step step = decode_step(bytes, length, offset);

        if (step.bytes == 0u || step.width < 0) break;
        if (step.bytes > budget - offset) break;
        offset += step.bytes;
        if (!needs_following_base(step.code)) accepted = offset;
    }
    return accepted;
}

/*
 * The sanitising is the fit, not a second escaping pass: kpa_cells_fit stops
 * at the first malformed sequence and at the first C0/DEL/C1 code point, so
 * the bytes copied here are inert as far as the terminal parser is
 * concerned and can go out verbatim.
 */
static void emit_row_text(kpa_cells_writer *writer, const char *text,
                          size_t length, int columns)
{
    size_t fitted;

    if (text == NULL || length == 0u) return;
    fitted = kpa_cells_fit(text, length, columns);
    if (fitted == 0u) return;
    if (fitted > (size_t)output_capacity - writer->used) {
        flush_buffer(writer);
        if (writer->failed) return;
    }
    if (fitted > (size_t)output_capacity - writer->used)
        fitted = fit_bytes(text, fitted,
                           (size_t)output_capacity - writer->used);
    emit_raw(writer, text, fitted);
}

kpa_cells_writer *kpa_cells_create(int output_fd)
{
    kpa_cells_writer *writer = calloc(1u, sizeof *writer);

    if (writer == NULL) return NULL;
    writer->fd = output_fd;
    writer->in_frame = false;
    writer->failed = output_fd < 0;
    writer->used = 0u;
    return writer;
}

void kpa_cells_destroy(kpa_cells_writer *writer)
{
    if (writer == NULL) return;
    flush_buffer(writer);
    free(writer);
}

/*
 * begin and end are guarded so the synchronised-update pair brackets a frame
 * exactly once.  An unbalanced CSI ?2026h leaves the terminal holding its
 * output until something else releases it.
 */
void kpa_cells_begin(kpa_cells_writer *writer)
{
    if (writer == NULL || writer->in_frame) return;
    writer->in_frame = true;
    emit_literal(writer, "\033[?2026h");
}

void kpa_cells_row(kpa_cells_writer *writer, int row, int column, int columns,
                   const char *text, size_t length, uint32_t rgb)
{
    if (writer == NULL) return;
    emit_literal(writer, "\033[");
    emit_number(writer, clamp_coordinate(row));
    emit_raw(writer, ";", 1u);
    emit_number(writer, clamp_coordinate(column));
    emit_raw(writer, "H", 1u);
    emit_literal(writer, "\033[38;2;");
    emit_number(writer, (rgb >> 16) & 0xFFu);
    emit_raw(writer, ";", 1u);
    emit_number(writer, (rgb >> 8) & 0xFFu);
    emit_raw(writer, ";", 1u);
    emit_number(writer, rgb & 0xFFu);
    emit_raw(writer, "m", 1u);
    emit_row_text(writer, text, length, columns);
    /*
     * Reset before erasing.  EL paints with the SGR background in force, so
     * clearing the tail after the reset clears it to the terminal's own
     * background instead of to whatever a caller left set.
     */
    emit_literal(writer, "\033[0m\033[K");
}

/*
 * Erases exactly `columns` cells from column 1 with ECH rather than erasing
 * to the end of the line, so a caller that owns a 40-column lyric gutter
 * cannot wipe out whatever the terminal is drawing to the right of it.
 */
void kpa_cells_clear_row(kpa_cells_writer *writer, int row, int columns)
{
    if (writer == NULL || columns <= 0) return;
    emit_literal(writer, "\033[");
    emit_number(writer, clamp_coordinate(row));
    emit_literal(writer, ";1H\033[0m\033[");
    emit_number(writer, clamp_coordinate(columns));
    emit_raw(writer, "X", 1u);
}

void kpa_cells_end(kpa_cells_writer *writer)
{
    if (writer == NULL || !writer->in_frame) return;
    writer->in_frame = false;
    emit_literal(writer, "\033[?2026l");
    flush_buffer(writer);
}
