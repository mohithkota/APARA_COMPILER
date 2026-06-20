// test_matmul.c — 3x3 matrix multiply using flattened 1D arrays (A[i][j] -> A[i*3+j])
// True 2D array syntax is avoided here: test_2d crashes the aligner (separate, undiagnosed
// pre-existing bug) and this sidesteps it entirely while testing the exact same arithmetic.
//
// Each cell of the result matrix is results[] directly -- see
// isa_coverage_tests/test_alu_full.c / compiler/STATUS.md 2026-06-20 for why.
#define N_RESULTS 9
long long results[N_RESULTS];

int A[9];
int B[9];

int main() {
    int i;
    int j;
    int k;
    int sum;

    A[0]=1; A[1]=2; A[2]=3;
    A[3]=4; A[4]=5; A[5]=6;
    A[6]=7; A[7]=8; A[8]=9;

    B[0]=9; B[1]=8; B[2]=7;
    B[3]=6; B[4]=5; B[5]=4;
    B[6]=3; B[7]=2; B[8]=1;

    for (i = 0; i < 3; i++) {
        for (j = 0; j < 3; j++) {
            sum = 0;
            for (k = 0; k < 3; k++) {
                sum = sum + A[i*3+k] * B[k*3+j];
            }
            results[i*3+j] = sum;
        }
    }

    return 1;
}
