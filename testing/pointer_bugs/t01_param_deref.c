int arr[4]={10,20,30,40}; long long results[2];
int f(int *p){ return p[0]+p[2]; }
int main(){ results[0]=f(arr); results[1]=f(arr+1); return 1; }
