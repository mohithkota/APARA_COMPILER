long long results[1];
long long __vreduce_vi32(long long a);
int main() { long long v = 0x0000000500000003; results[0] = __vreduce_vi32(v); return 1; }
