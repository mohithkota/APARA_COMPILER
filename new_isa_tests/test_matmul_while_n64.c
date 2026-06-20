// N=64: each row is 64 elements = exactly 4 u128 chunks (no padding needed,
// 64 is already a multiple of 16). Full dot product = sum of 4
// __dot128_direct_vu8 calls (elements 0-15, 16-31, 32-47, 48-63).
vu8_t A[4096];
vu8_t BT[4096];
long long C[4096];

int main() {
    int i;
    int j;

    i = 0;
    while (i < 64) {
        j = 0;
        while (j < 64) {
            A[i*64+j]  = (i*64+j+1) % 256;
            BT[i*64+j] = (j*64+i+1) % 256;
            j++;
        }
        i++;
    }

    i = 0;
    while (i < 64) {
        j = 0;
        while (j < 64) {
            C[i*64+j] = __dot128_direct_vu8(&A[i*64],    &BT[j*64])
                      + __dot128_direct_vu8(&A[i*64+16], &BT[j*64+16])
                      + __dot128_direct_vu8(&A[i*64+32], &BT[j*64+32])
                      + __dot128_direct_vu8(&A[i*64+48], &BT[j*64+48]);
            j++;
        }
        i++;
    }

    if (C[0] != 206880)     return -1;   // row0,col0
    if (C[63] != 198656)    return -2;   // row0,col63
    if (C[4095] != 1378304) return -3;   // row63,col63
    if (C[2900] != 727712)  return -4;   // row45,col20 -- was in the stack/global
                                          // overlap zone before --stack-top was fixed
    return 1;
}
