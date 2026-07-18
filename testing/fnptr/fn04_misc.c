/* fn04: &func syntax, fp returned from a function, fp comparison, global fp */
long long results[4];

long long inc(long long x) { return x + 1; }
long long dec(long long x) { return x - 1; }

long long (*pick(int up))(long long) { return up ? inc : dec; }

long long (*gfp)(long long);

int main() {
    long long (*f)(long long) = &inc;      /* &func form */
    results[0] = (*f)(41);                 /* 42 */
    f = pick(0);                           /* fp returned from a call */
    results[1] = f(10);                    /* 9 */
    results[2] = (f == dec) ? 1 : 2;       /* 1: fp equality vs func name */
    gfp = pick(1);                         /* global fp variable */
    results[3] = gfp(6);                   /* 7 */
    return 1;
}
