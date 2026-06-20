#define N_RESULTS 6
long long results[N_RESULTS];

long long a=10, b=20, c=10;

int main() {
    if (a < b) results[0] = 1;
    else results[0] = 99;

    if (a > b) results[1] = 1;
    else results[1] = 99;

    if (a == c) results[2] = 1;
    else results[2] = 99;

    if (a != b) results[3] = 1;
    else results[3] = 99;

    if (a >= c) results[4] = 1;
    else results[4] = 99;

    if (a <= b) results[5] = 1;
    else results[5] = 99;

    return 1;
}
