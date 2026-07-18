/* fn01: basic function pointer — assign, call, reassign */
long long results[4];

long long add(long long a, long long b) { return a + b; }
long long sub(long long a, long long b) { return a - b; }

int main() {
    long long (*fp)(long long, long long);
    fp = add;
    results[0] = fp(10, 3);        /* 13 */
    fp = sub;
    results[1] = fp(10, 3);        /* 7 */
    results[2] = (*fp)(100, 42);   /* 58 */
    fp = add;
    results[3] = fp(fp(1, 2), 4);  /* 7 */
    return 1;
}
