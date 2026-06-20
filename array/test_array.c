int arr[5] = {10, 20, 30, 40, 50};

#define N_RESULTS 6
long long results[N_RESULTS];

int main() {
    int i;
    int result;
    result = 0;
    for (i = 0; i < 5; i++) {
        results[i] = arr[i];
        result = result + arr[i];
    }
    results[5] = result;
    return 1;
}
