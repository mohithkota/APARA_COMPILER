int arr[5]={10,20,30,40,50}; long long results[2];
int main(){ int *p=arr; int *q=arr+3; results[0]=(long long)(q-p); results[1]=(long long)(q-arr); return 1; }
