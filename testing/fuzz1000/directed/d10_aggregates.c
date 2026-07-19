/* d10: 2D/3D arrays, nested structs, unions, bit-fields, enums, designated inits */
long long results[8];
long long m3[2][2][3] = {{{1,2,3},{4,5,6}},{{7,8,9},{10,11,12}}};
long long dz[6] = {[2] = 30, [5] = 50};
struct In { long long a; long long b; };
struct Out { struct In in; long long c; };
struct Out so = {{7, 8}, 9};
union U { long long ll; };
struct BF { unsigned a : 4; unsigned b : 6; unsigned c : 12; };
enum E { E1 = 3, E2 = 17, E3 };
int main() {
    long long s = 0;
    for (int i = 0; i < 2; i++)
        for (int j = 0; j < 2; j++)
            for (int k = 0; k < 3; k++)
                s += m3[i][j][k] * (i + j + k + 1);
    results[0] = s;
    results[1] = dz[0] + dz[2] * 2 + dz[5];
    results[2] = so.in.a * 10 + so.in.b + so.c * 100;
    so.in.b = 42; results[3] = so.in.b - so.in.a;
    union U u; u.ll = -777; results[4] = u.ll + 1000;
    struct BF bf; bf.a = 9; bf.b = 33; bf.c = 2000;
    results[5] = bf.a + bf.b * 16 + bf.c;
    results[6] = E1 + E2 * 2 + E3 * 3;
    static long long persist = 5; persist *= 2;
    results[7] = persist;
    return 1;
}
