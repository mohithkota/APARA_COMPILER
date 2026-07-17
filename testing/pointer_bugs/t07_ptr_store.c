int arr[4]={0,0,0,0}; long long results[3];
int main(){ int *p=arr; *p=11; p[2]=33; *(p+1)=22; results[0]=arr[0]; results[1]=arr[1]; results[2]=arr[2]; return 1; }
