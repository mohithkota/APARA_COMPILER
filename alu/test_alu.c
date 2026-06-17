long long a = 10;
long long b = 3;

long long add_res = 0;
long long sub_res = 0;
long long mul_res = 0;
long long div_res = 0;
long long mod_res = 0;
long long and_res = 0;
long long or_res  = 0;
long long xor_res = 0;
long long shl_res = 0;
long long shr_res = 0;

int main() {
    add_res = a + b;
    sub_res = a - b;
    mul_res = a * b;
    div_res = a / b;
    mod_res = a % b;
    and_res = a & b;
    or_res  = a | b;
    xor_res = a ^ b;
    shl_res = a << b;
    shr_res = a >> b;
    return add_res;
}
