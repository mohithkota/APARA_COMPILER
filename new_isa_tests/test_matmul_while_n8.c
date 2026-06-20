// N=8: each row is only 8 elements, but __dot128_direct_vu8 always reads a
// full 16-byte u128. Each row is stored in its own 16-byte-stride slot
// (only bytes 0-7 are real data, bytes 8-15 are explicitly zero-padded) so
// a single __dot128_direct_vu8 call still gives the exact correct 8-element
// dot product -- the extra 8 zero-padded terms contribute nothing to the sum.
vu8_t A[128];
vu8_t BT[128];
long long C[64];

int main() {
    int i;
    int j;

    i = 0;
    while (i < 8) {
        j = 0;
        while (j < 8) {
            A[i*16+j]  = (i*8+j+1) % 256;
            BT[i*16+j] = (j*8+i+1) % 256;
            j++;
        }
        j = 8;
        while (j < 16) {
            A[i*16+j]  = 0;
            BT[i*16+j] = 0;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 8) {
        j = 0;
        while (j < 8) {
            C[i*8+j] = __dot128_direct_vu8(&A[i*16], &BT[j*16]);
            j++;
        }
        i++;
    }

    if (C[0] != 1380)   return -1;   // 0x564, row0,col0
    if (C[7] != 1632)   return -2;   // 0x660, row0,col7
    if (C[63] != 17760) return -3;   // 0x4560, row7,col7
    return 1;
}
