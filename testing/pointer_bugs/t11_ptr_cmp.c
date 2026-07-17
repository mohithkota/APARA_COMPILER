int arr[4]={10,20,30,40}; long long results[3];
int main(){ int *p=arr; int *q=arr+2; results[0]=(p<q); results[1]=(p==arr); results[2]=(q>p); return 1; }
