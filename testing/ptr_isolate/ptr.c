int arr[6] = {10,20,30,40,50,60};
long long results[12];
int sumn(int *p, int n){ int s=0,i; for(i=0;i<n;i++) s+=p[i]; return s; }
int main(){
    int *p = arr;
    results[0]  = arr[3];        /* direct index          -> 40 (works) */
    results[1]  = p[0];          /* local ptr, index 0    -> 10 */
    results[2]  = p[3];          /* local ptr, index 3    -> 40 */
    results[3]  = *p;            /* deref base            -> 10 */
    results[4]  = *(p+3);        /* deref ptr+offset      -> 40 */
    results[5]  = *(arr+3);      /* deref arr+offset      -> 40 */
    results[6]  = p[2]-p[0];     /* two local-ptr indexes -> 20 */
    results[7]  = sumn(arr, 6);  /* pass base             -> 210 */
    results[8]  = sumn(arr+2, 3);/* pass arr+offset       -> 120 */
    results[9]  = sumn(p, 6);    /* pass local ptr        -> 210 */
    int *q = arr+2;
    results[10] = q[0];          /* ptr initialized w/ offset -> 30 */
    results[11] = *q;            /* deref that ptr        -> 30 */
    return 1;
}
