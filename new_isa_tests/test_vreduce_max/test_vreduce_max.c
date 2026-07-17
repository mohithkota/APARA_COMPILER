/* Horizontal-max vector reduce ($vreduce $max). ADD reduce included as a
   control. MAX verified working on the fixed toolchain (MIN/MUL/OR/XOR/AND/
   XNOR reduce return 0 in the simulator and are intentionally not emitted). */
long long results[3];
long long __vreduce_vi8(long long a);
long long __vreduce_max_vi8(long long a);
long long __vreduce_max_vi32(long long a);
int main() {
    long long v8  = 0x0807060504030201;   /* vi8 lanes 1..8 */
    long long v32 = 0x0000000500000003;   /* vi32 lanes 3,5 */
    results[0] = __vreduce_vi8(v8);        /* sum = 36 */
    results[1] = __vreduce_max_vi8(v8);    /* max = 8  */
    results[2] = __vreduce_max_vi32(v32);  /* max = 5  */
    return 1;
}
