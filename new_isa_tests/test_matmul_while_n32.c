// N=32: each row is 32 elements = exactly 2 u128 chunks (no padding needed,
// 32 is already a multiple of 16). Full dot product = sum of 2
// __dot128_direct_vu8 calls (elements 0-15, then 16-31).
vu8_t A[1024];
vu8_t BT[1024];
long long C[1024];

int main() {
    int i;
    int j;

    i = 0;
    while (i < 32) {
        j = 0;
        while (j < 32) {
            A[i*32+j]  = (i*32+j+1) % 256;
            BT[i*32+j] = (j*32+i+1) % 256;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 32) {
        j = 0;
        while (j < 32) {
            C[i*32+j] = __dot128_direct_vu8(&A[i*32], &BT[j*32])
                      + __dot128_direct_vu8(&A[i*32+16], &BT[j*32+16]);
            j++;
        }
        i++;
    }

    if (C[0] != 65040)     return -1;   // row0,col0
    if (C[31] != 60928)    return -2;   // row0,col31
    if (C[1023] != 863744) return -3;   // row31,col31
    return 1;
}
