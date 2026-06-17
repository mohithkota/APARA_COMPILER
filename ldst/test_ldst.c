long long src_a = 1000;
long long src_b = 2000;
long long src_c = 3000;
long long src_d = 4000;

long long out_copy      = 0;
long long out_add       = 0;
long long out_sub       = 0;
long long out_mul       = 0;
long long out_overwrite = 0;

int main() {
    out_copy = src_a;

    out_add = src_a + src_b;

    out_sub = src_d - src_c;

    out_mul = src_a * src_b;

    out_overwrite = src_a;
    out_overwrite = src_b;

    return out_copy;
}
