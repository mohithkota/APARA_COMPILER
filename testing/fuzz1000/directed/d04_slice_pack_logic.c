/* d04: $slice, $pack, and the ~& ~| ~^ ALU ops */
long long results[6];
long long __slice(long long, int, int);
long long __pack(long long, long long, int, int);
long long __nand(long long, long long); long long __nor(long long, long long);
long long __xnor(long long, long long);
long long ga = 0x123456789abcdef0LL, gb = 0x0f0f0f0f0f0f0f0fLL;
int main() {
    results[0] = __slice(ga, 31, 16);
    results[1] = __slice(ga, 63, 48) + __slice(gb, 7, 0);
    results[2] = __pack(ga, gb, 32, 16);   /* packed_nbits must be a multiple of word_nbits */
    results[3] = __nand(ga, gb) & 0xffffffffLL;
    results[4] = __nor(ga, gb) & 0xffffffffLL;
    results[5] = __xnor(ga, gb) & 0xffffffffLL;
    return 1;
}
