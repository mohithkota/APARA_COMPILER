int arr[4]={0,0,0,0}; long long results[2];
void setit(int *p,int v){ p[0]=v; p[1]=v+1; }
int main(){ setit(arr,55); results[0]=arr[0]; results[1]=arr[1]; return 1; }
