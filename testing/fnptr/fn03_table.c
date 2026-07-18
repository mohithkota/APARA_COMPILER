/* fn03: dispatch table — array of function pointers, indexed calls */
long long results[4];

long long op_add(long long a, long long b) { return a + b; }
long long op_sub(long long a, long long b) { return a - b; }
long long op_mul(long long a, long long b) { return a * b; }
long long op_min(long long a, long long b) { return a < b ? a : b; }

int main() {
    long long (*ops[4])(long long, long long);
    ops[0] = op_add; ops[1] = op_sub; ops[2] = op_mul; ops[3] = op_min;
    for (int i = 0; i < 4; i++)
        results[i] = ops[i](12, 5);   /* 17, 7, 60, 5 */
    return 1;
}
