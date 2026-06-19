// Stage 1: u128 load mechanics only. Load one 128-bit value from src[0..1]
// into a register pair via __ld128(dst, src), store it back out as two
// u64 halves into dst[0..1]. No dot product, no matmul -- just proves the
// load+pair-allocation mechanism. Expect dst[0]=src[0]=0x1111..., dst[1]=
// src[1]=0x2222... (hypothesis: lower register = lower address, matching
// every other multi-register convention in this compiler -- verified below,
// not assumed).
long long src[2];
long long dst[2];

int main() {
    src[0] = 0x1111111111111111LL;
    src[1] = 0x2222222222222222LL;

    __ld128(dst, src);

    if (dst[0] != 0x1111111111111111LL) return -1;
    if (dst[1] != 0x2222222222222222LL) return -2;
    return 1;
}
