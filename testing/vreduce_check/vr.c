long long results[1];
long long __vreduce_vi8(long long a);
int main() {
    long long v = 0x0807060504030201;   /* bytes 1..8 as vi8 */
    results[0] = __vreduce_vi8(v);
    return 1;
}
