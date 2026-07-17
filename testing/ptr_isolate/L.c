int arr[6]={10,20,30,40,50,60}; long long results[3];
int main(){
    int *p = arr;
    results[0] = p[0];     /* 10 */
    results[1] = p[3];     /* 40 */
    results[2] = (long long)(p - arr);  /* 0 (sanity: is p==arr?) */
    return 1;
}
