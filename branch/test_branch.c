long long a=10, b=20, c=10;
long long res1=0, res2=0, res3=0, res4=0, res5=0, res6=0;

int main() {
    if (a < b) res1 = 1;
    else res1 = 99;

    if (a > b) res2 = 1;
    else res2 = 99;

    if (a == c) res3 = 1;
    else res3 = 99;

    if (a != b) res4 = 1;
    else res4 = 99;

    if (a >= c) res5 = 1;
    else res5 = 99;

    if (a <= b) res6 = 1;
    else res6 = 99;

    return res1;
}
