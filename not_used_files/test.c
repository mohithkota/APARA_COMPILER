int arrayA[4] = {1, 2, 3, 4};
int arrayB[4] = {5, 6, 7, 8};
int final_result = 0;

int main() {
    int sum = 0, i;
    for (i = 0; i < 4; i++) {
        sum += arrayA[i] * arrayB[i];
    }
    final_result = sum;
    return sum;
}
