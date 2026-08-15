int data[5] = {3, 9, 2, 7, 5};
long long results[1];
int main() {
    int i;
    int best = data[0];
    for (i = 1; i < 5; i++) {
        if (data[i] > best) { best = data[i]; }
    }
    results[0] = best;
    return 1;
}
