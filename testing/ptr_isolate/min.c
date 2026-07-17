int arr[6] = {10,20,30,40,50,60};
long long results[1];
int sumn(int *p, int n){ int s=0,i; for(i=0;i<n;i++) s+=p[i]; return s; }
int main(){ results[0]=sumn(arr,6); return 1; }
